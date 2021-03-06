import torch
from torch import nn
import torch.nn.functional as F
from models.utils import param, reverse_packed_sequence, Namespace, recursiveXavier, addTimeSignal, getSignal, rnnPlusWarmupDecay
from torch.nn.utils.rnn import PackedSequence, pad_packed_sequence, pack_padded_sequence
import pytorch_lightning as pl
from torch.optim.lr_scheduler import LambdaLR

def smoothingLoss(preds, target, eps=0.1):
    n = preds.size(-1)
    log_preds = F.log_softmax(preds, dim=-1)
    loss = - log_preds.sum(dim=-1).mean()
    nll = F.nll_loss(log_preds, target, reduction='mean')
    return eps * loss / n + (1-eps) * nll

class CNNEmbedding(nn.Module):
    """Model that takes input of shape [n_words, n_chars] and returns char embedding of shape [n_words, embeddingDim]
       
        Parameters: 
            numChars(int): total chars(93)
            embeddingDim(int): use default 128
    """
    def __init__(self, numChars, embeddingDim):
        super().__init__()
        self.embedding = nn.Embedding(numChars, embeddingDim)
        
        # init zero index to a large negative value
        self.embedding.weight.data[0] = 0
        
        self.conv1d = nn.Conv1d(embeddingDim, embeddingDim, 3, 1, 1, bias=False)
    
    def forward(self, sequences, mask=None): # packed with shape (pack_len w) or p w
        x = self.embedding(sequences.data) # p w u
        x = self.conv1d( x.permute(0, 2, 1) ).permute(0, 2, 1) # conv doesnt change shape; p w u
                
        # x is still p w u we have to take max across each word
        # big brain time for non zero maxing
        x, _ = torch.max(x + mask, 1) # max is across each word
        _, *args = sequences
        return PackedSequence(x, *args)
    


class TransitionGRU(nn.Module):
    def __init__(self, hidden_units):
        super().__init__()
        # save config
        self.hidden_units = hidden_units
        
        self.reset_gate     = param(hidden_units, hidden_units)
        self.reset_norm     = nn.LayerNorm(hidden_units)

        self.update_gate    = param(hidden_units, hidden_units)
        self.update_norm    = nn.LayerNorm(hidden_units)

        self.candidate_gate = nn.Linear(hidden_units, hidden_units)
        self.candidate_drop = nn.Dropout(0.3)
        
    
    def forward(self, ht):
        """
        Special GRU that uses on hidden state as input
        Expected format is b u 
            r = sigma(layernorm(Wr * h))
            z = sigma(layernorm(Wz * h))
            
            n = tanh (r .* (Wn * h + bn))
            n = dropout(n)

            y = (1 - z) * h + z * n
        """
        # reset gate xt -> (b u) (u u) -> (b u)
        r = torch.mm(ht, self.reset_gate)
        r = self.reset_norm(r)
        r = torch.sigmoid(r)
        
        # update gate
        z = torch.mm(ht, self.update_gate)
        z = self.update_norm(z)
        z = torch.sigmoid(z)
        
        # candidate state
        n = r * self.candidate_gate(ht)
        n = torch.tanh(n)
        n = self.candidate_drop(n)
        
        out = (1-z) * n  +  z * ht
        return out
    

class LinearEnchancedGRU(nn.Module):
    def __init__(self, input_units, hidden_units):
        super().__init__()
        # save config
        self.input_units = input_units
        self.hidden_units = hidden_units
        
        # weights for connecting input to a gate (3 gates)
        cat_units = input_units + hidden_units
        self.reset_gate  = param(cat_units, hidden_units)
        self.reset_norm  = nn.LayerNorm(hidden_units)

        self.update_gate = param(cat_units, hidden_units)
        self.update_norm = nn.LayerNorm(hidden_units)

        # extra params for linear enhancement
        self.linear_gate = param(cat_units, hidden_units)
        self.linear_transform = param(input_units, hidden_units)
        self.linear_norm = nn.LayerNorm(hidden_units)

        # weights to connect input to candidate activation
        self.Cx = param(input_units, hidden_units)
        
        # bias true for linear_state in GCDT paper
        self.Ch = nn.Linear(hidden_units, hidden_units) 
        self.candidate_drop = nn.Dropout(0.3)

    def forward(self, x, hx=None):
        # expected format is b u
        if hx is None:
            hx = torch.zeros(x.size(0), self.hidden_units, dtype=x.dtype, device=x.device)
        
        hx = hx[:x.size(0)]
        concat_out = torch.cat([x, hx], dim=-1)
        
        # reset gate
        r = torch.mm(concat_out, self.reset_gate)
        r = self.reset_norm(r)
        r = torch.sigmoid(r)
        
        # update gate
        z = torch.mm(concat_out, self.update_gate)
        z = self.update_norm(z)
        z = torch.sigmoid(z)
        
        # linear enhanced gate
        l = torch.mm(concat_out, self.linear_gate)
        l = self.linear_norm(l)
        l = torch.sigmoid(l)
        
        # candidate state
        n = torch.mm(x, self.Cx) + r * self.Ch(hx)
        n = torch.tanh(n) + l * torch.mm(x, self.linear_transform)
        n = self.candidate_drop(n)
        # linear combination
        ht = (1-z) * hx  +  z * n
        return ht

class DeepTransitionRNN(nn.Module):
    def __init__(self, inputUnits, outputUnits, transitionNumber):
        super().__init__()

        self.linearGRU = LinearEnchancedGRU(inputUnits, outputUnits)

        tgru = [TransitionGRU(outputUnits)  for _ in range(transitionNumber)]
        self.transitionGRU = nn.Sequential(*tgru)
    
    def cell_forward(self, xt, ht):
        ht = self.linearGRU(xt, ht)
        return self.transitionGRU(ht)

    def forward(self, sequence):
        inputSequence, batchSizes, sortedIndices, unsortedIndices = sequence
        start = 0
        ht = None
        outputs = []
        for batch in batchSizes:
            xt = inputSequence[start:start+batch]
            ht = self.cell_forward(xt, ht)
            outputs.append(ht)
            start = start + batch
        outputs = torch.cat(outputs)
        return PackedSequence(outputs, batchSizes, sortedIndices, unsortedIndices)


class SequenceLabelingEncoderDecoder(pl.LightningModule):
    def __init__(self, inputUnits, encoderUnits, decoderUnits, transitionNumber, numTags):
        super().__init__()
        # save config
        self.inputUnits = inputUnits
        self.encoderUnits = encoderUnits
        self.decoderUnits = decoderUnits
        self.transitionNumber = transitionNumber
        self.tarEmbUnits = 300

        # encoder is bidirectional, but decoder is unidirectional
        self.targetEmbedding = nn.Embedding(numTags, self.tarEmbUnits)
        self.targetDropout   = nn.Dropout(.5) 
        self.targetBias      = nn.Parameter(torch.zeros(1, self.tarEmbUnits))

        self.fowardEncoder   = DeepTransitionRNN(inputUnits, encoderUnits, transitionNumber)
        self.backwardEncoder = DeepTransitionRNN(inputUnits, encoderUnits, transitionNumber)
        self.postEncode      = nn.Sequential(nn.Linear(2*encoderUnits, decoderUnits), nn.Tanh())

        self.decoder         = DeepTransitionRNN(2*encoderUnits + self.tarEmbUnits, decoderUnits, transitionNumber)
        self.output          = nn.Sequential(
                                    nn.Linear(decoderUnits + self.tarEmbUnits, decoderUnits),
                                    nn.Tanh(),
                                    nn.Dropout(0.5),
                                    nn.Linear(decoderUnits, numTags)
                                )
    
    def encode(self, sequence):
        reversedSequence = reverse_packed_sequence(sequence)
        forwardEncoded  = self.fowardEncoder(sequence)
        backwardEncoded = self.backwardEncoder(reversedSequence)
        reversedBackwardEncoded = reverse_packed_sequence(backwardEncoded)
        encoded = torch.cat([forwardEncoded.data, reversedBackwardEncoded.data], dim=-1)
        return encoded
    
    def get_decoder_initial_state(self, encoded, *argsForPackedSequence):
        """
            Mean pools encoded across time
        """
        # deal encoded as packed sequence
        encoded = PackedSequence(encoded, *argsForPackedSequence)

        # first pad the encoded
        encoded, lens = pad_packed_sequence(encoded)

        # encode.sum will have shape b, u and lens has shape (t, )
        # expand shape to allow broadcast division
        denominator = torch.unsqueeze(lens, -1).to(self.device)

        # sum and divide to get mean
        x = encoded.sum(dim=0) / denominator

        # return the result of linear + tanh function
        return self.postEncode(x)
    
    def decode_once(self, encoded, prevTarget, hiddenState):
        prevTarget = self.targetDropout(prevTarget + self.targetBias)
        inputState = torch.cat([encoded, prevTarget], dim=-1)
        hiddenState = self.decoder.cell_forward(inputState, hiddenState)

        featuresForLinear = torch.cat([hiddenState, prevTarget], dim=-1)
        logit = self.output(featuresForLinear)
        return hiddenState, logit

    def enforced_logits(self, sequence:PackedSequence, targets:PackedSequence):
        encoded = self.encode(sequence)

        # encoded is a tensor here and not a packed sequence
        start = 0
        logits = []
        prevTarget = torch.zeros(sequence.batch_sizes[0].item(), self.tarEmbUnits).to(self.device)
        targetVectors = self.targetEmbedding(targets.data)
        targetVectors = PackedSequence(targetVectors, *sequence[1:])
        targetVectors = addTimeSignal(targetVectors, self.device).data

        hiddenState = self.get_decoder_initial_state(encoded, *sequence[1:])
        for batch in sequence.batch_sizes:
            hiddenState, logit = self.decode_once(
                encoded[start:start+batch],
                prevTarget[:batch],
                hiddenState
            )
            # update prevTarget after processing current timestep
            prevTarget = targetVectors[start:start+batch]
            logits.append(logit)
            start = start + batch
        logits = torch.cat(logits)

        logits = PackedSequence(logits, *sequence[1:])
        return logits


"""
    Glove and char embeddings to global embeddings
"""
class GlobalContextualEncoder(pl.LightningModule):
    def __init__(self, numChars, charEmbedding, numWords, wordEmbedding, outputUnits, transitionNumber):
        super().__init__()
        # no dropout for cnn
        self.cnn   = CNNEmbedding(numChars, charEmbedding)
        self.outerCnn = CNNEmbedding(numChars, charEmbedding)
        
        self.glove = nn.Embedding(numWords, wordEmbedding)
        self.gloveBias = nn.Parameter(torch.zeros(1, wordEmbedding))
        self.srcDrop = nn.Dropout(0.5)
        
        self.outerGloveBias = nn.Parameter(torch.zeros(1, wordEmbedding))
        self.outerSrcDrop = nn.Dropout(0.5)
        
        self.glove.weight.requires_grad=False
        
        encoderInputUnits = wordEmbedding + charEmbedding
        self.forwardEncoder  = DeepTransitionRNN(encoderInputUnits, outputUnits, transitionNumber)
        self.backwardEncoder = DeepTransitionRNN(encoderInputUnits, outputUnits, transitionNumber)
        
    def forward(self, words, chars, charMask):
        _, *args = words
        origW = self.glove(words.data)

        w = origW + self.gloveBias
        c = self.cnn(chars, charMask).data
        
        # word and char concat, pass through encoder and we get directional global context
        wc = torch.cat([w, c], dim=-1)
        forwardInput  = PackedSequence( wc, *args )

        # add time signal then dropout
        forwardInput = addTimeSignal(forwardInput, self.device)
        forwardInput = PackedSequence(self.srcDrop(forwardInput.data), *args)

        forwardG  = self.forwardEncoder(forwardInput)
        
        backwardInput = reverse_packed_sequence(forwardInput)      
        backwardG = self.backwardEncoder(backwardInput)
        backwardG = reverse_packed_sequence(backwardG)
        
        nonDirectionalG = torch.cat([forwardG.data, backwardG.data], dim=-1)
        
        # mean pooling is done by padding with zeros, taking timewise sum and dividing by lengths
        nonDirectionalG = PackedSequence(nonDirectionalG, *args)
        nonDirectionalG, lens = pad_packed_sequence(nonDirectionalG)


        # expand shape to allow broadcast division
        denominator = torch.unsqueeze(torch.unsqueeze(lens, -1), 0).to(self.device)

        # sum and divide to get mean
        nonDirectionalG_sum = nonDirectionalG.sum(dim=0, keepdim=True)
        g = nonDirectionalG_sum / denominator
        
        # need to broadcast g and concat with wc
        new_shape = [nonDirectionalG.data.shape[0] // g.shape[0]] + [-1] * (len(g.shape) - 1)
        g = pack_padded_sequence(g.expand(*new_shape), lens, enforce_sorted=False)
        
        
        # this is meant to go with the global embedding
        w = origW + self.outerGloveBias
        c = self.outerCnn(chars, charMask).data
        wcg = torch.cat([w, c, g.data], dim=-1)
        wcg = PackedSequence(wcg, *args)

        # add time signal then dropout
        wcg = addTimeSignal(wcg, self.device)
        wcg = PackedSequence(self.outerSrcDrop(wcg.data), *args)
        
        return wcg

"""
    Model from the paper
"""
class GlobalContextualDeepTransition(pl.LightningModule):
    def __init__(self, numChars, charEmbedding, numWords,
                     wordEmbedding, contextOutputUnits, contextTransitionNumber,
                        encoderUnits, decoderUnits, transitionNumber, numTags, learning_rate=8e-4):
        super().__init__()
        self.numTags = numTags
        self.learning_rate = learning_rate
        self.contextEncoder = GlobalContextualEncoder(numChars, charEmbedding, numWords,
                                                          wordEmbedding, contextOutputUnits, contextTransitionNumber)
        self.labellerInput = wordEmbedding + charEmbedding + 2 * contextOutputUnits # units in g
        self.sequenceLabeller = SequenceLabelingEncoderDecoder(self.labellerInput, encoderUnits, decoderUnits, transitionNumber, numTags)

    def init_weights(self, gloveEmbedding):
        self.apply(recursiveXavier)
        self.contextEncoder.glove.weight = torch.nn.Parameter(gloveEmbedding, requires_grad=False)

    def smoothingLoss(self, preds, target, epsilon=0.1):
        numTags = preds.size(-1)
        logPreds = F.log_softmax(preds, dim=-1)
        loss = - logPreds.sum(dim=-1).mean()
        nll = F.nll_loss(logPreds, target, reduction='mean')
        loss = epsilon * loss/numTags + (1 - epsilon) * nll
        return loss
    
    def encode(self, words, chars, charMask):
        """
            Encodes the batch, inits the hiddenState and prevTarget(y_{t-1})
        """
        wcg = self.contextEncoder(words, chars, charMask)
        encoded = self.sequenceLabeller.encode(wcg)
        hiddenState = self.sequenceLabeller.get_decoder_initial_state(encoded, *wcg[1:])
        prevTarget  = torch.zeros(
                        words.batch_sizes[0].item(), # number of examples in the batch
                        self.sequenceLabeller.tarEmbUnits
                    ).to(self.device)
        return encoded, hiddenState, prevTarget
    
    def testForward(self, batch):
        # encode everything and init vars for decoder
        words, chars, charMask = batch
        encoded, hiddenState, prevTarget = self.encode(batch)

        start = 0
        preds = []
        for timeBias, b in enumerate(words.batch_sizes):
            # refresh = start == 0
            # logits = self.inferOneStep(words, chars, charMask, prevTarget, refresh)
            u = prevTarget.shape[1]
            prevTarget = prevTarget[:b] + getSignal(1, u, timeBias, self.device)
            hiddenState, logits = self.sequenceLabeller.decode_once(
                encoded[start:start+b],
                prevTarget,
                hiddenState
            )
            
            # include logic here for beam search to expand
            prevTarget = logits.argmax(axis=1)

            preds.append(prevTarget)
            prevTarget = self.sequenceLabeller.targetEmbedding(prevTarget)
            start += b
        preds = torch.cat(preds)
        return PackedSequence(preds, *words[1:])

    def training_step(self, batch, batch_idx):
        words, chars, charMask, targets = batch
        
        # compute the global representation and concat with word and char representations
        wcg = self.contextEncoder(words, chars, charMask)
        
        # encode concaatentated input and decode logits using sequence labeller
        logits = self.sequenceLabeller.enforced_logits(wcg, targets)
        
        # compute loss using averaging loss across different timesteps
        loss = self.smoothingLoss(logits.data, targets.data)
        
        # log the loss so we can use it for ckpt during training
        self.log('train_loss', loss)
        
        return loss

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.learning_rate)
        scheduler = {
            'scheduler': LambdaLR(optimizer, rnnPlusWarmupDecay(self.learning_rate)),
            'interval': 'step',
            'frequency': 1,
        }
        return [optimizer], [scheduler]