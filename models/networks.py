import torch
from torch import nn
import torch.nn.functional as F
from models.utils import param, reverse_packed_sequence, Namespace, recursiveXavier
from torch.nn.utils.rnn import PackedSequence, pad_packed_sequence, pack_padded_sequence
import pytorch_lightning as pl

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
        
        self.conv1d = nn.Conv1d(embeddingDim, embeddingDim, 3, 1, 1)
    
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


class SequenceLabelingEncoderDecoder(nn.Module):
    def __init__(self, inputUnits, encoderUnits, decoderUnits, transitionNumber, numTags):
        super().__init__()
        # save config
        self.inputUnits = inputUnits
        self.encoderUnits = encoderUnits
        self.decoderUnits = decoderUnits
        self.transitionNumber = transitionNumber
        
        # encoder is bidirectional, but decoder is unidirectional
        self.targetEmbedding = nn.Sequential(
                                    nn.Embedding(numTags, decoderUnits),
                                    nn.Dropout(0.5)
                                )
        self.fowardEncoder   = DeepTransitionRNN(inputUnits, encoderUnits, transitionNumber)
        self.backwardEncoder = DeepTransitionRNN(inputUnits, encoderUnits, transitionNumber)
        self.decoder         = DeepTransitionRNN(2*encoderUnits, decoderUnits, transitionNumber)
        self.output          = nn.Sequential(
                                    nn.Linear(decoderUnits, decoderUnits),
                                    nn.Tanh(),
                                    nn.Dropout(0.5),
                                    nn.Linear(decoderUnits, numTags)
                                )
        
    def enforced_logits(self, sequence, targets):
        data, batchSizes, sortedIndices, unsortedIndices = sequence
        reversedSequence = reverse_packed_sequence(sequence)
        forwardEncoded  = self.fowardEncoder(sequence)
        backwardEncoded = self.backwardEncoder(reversedSequence)
        reversedBackwardEncoded = reverse_packed_sequence(backwardEncoded)
        encoded = torch.cat([forwardEncoded.data, reversedBackwardEncoded.data], dim=-1)

        # encoded is a tensor here and not a packed sequence
        start = 0
        logits = []
        prevTarget = None
        targetVectors = self.targetEmbedding(targets.data)
        for batch in batchSizes:
            xt = encoded[start:start+batch]
            ht = self.decoder.cell_forward(xt, prevTarget)
            logit = self.output(ht)
            logits.append(logit)
            
            # update prevTarget after processing current timestep
            prevTarget = targetVectors[start:start+batch]
            start = start + batch
        logits = torch.cat(logits)
        return logits


"""
    Glove and char embeddings to global embeddings
"""
class GlobalContextualEncoder(nn.Module):
    def __init__(self, numChars, charEmbedding, numWords, wordEmbedding, outputUnits, transitionNumber):
        super().__init__()
        self.cnn   = CNNEmbedding(numChars, charEmbedding)
        self.cnnDrop = nn.Dropout(0.5)
        
        self.glove = nn.Embedding(numWords, wordEmbedding)
        self.gloveBias = nn.Parameter(torch.zeros(1, wordEmbedding))
        self.gloveDrop = nn.Dropout(0.5)
                    
        self.glove.weight.requires_grad=False
        
        encoderInputUnits = wordEmbedding + charEmbedding
        self.forwardEncoder  = DeepTransitionRNN(encoderInputUnits, outputUnits, transitionNumber)
        self.backwardEncoder = DeepTransitionRNN(encoderInputUnits, outputUnits, transitionNumber)
        
    def forward(self, words, chars, charMask):
        _, *args = words
        
        w = self.glove(words.data) + self.gloveBias
        w = self.gloveDrop(w)

        c = self.cnn(chars, charMask)
        c = self.cnnDrop(c.data)
        
        # word and char concat, pass through encoder and we get directional global context
        wc = torch.cat([w, c], dim=-1)
        forwardInput  = PackedSequence( wc, *args )
        forwardG  = self.forwardEncoder(forwardInput)
        
        backwardInput = reverse_packed_sequence(forwardInput)      
        backwardG = self.backwardEncoder(backwardInput)
        backwardG = reverse_packed_sequence(backwardG)
        
        nonDirectionalG = torch.cat([forwardG.data, backwardG.data], dim=-1)
        
        # mean pooling is done by padding with zeros, taking timewise sum and dividing by lengths
        nonDirectionalG = PackedSequence(nonDirectionalG, *args)
        nonDirectionalG, lens = pad_packed_sequence(nonDirectionalG)

        # here we see what
        device = next(self.parameters()).device
        lens = torch.unsqueeze(torch.unsqueeze(lens, -1), 0).to(device)

        nonDirectionalG_sum = nonDirectionalG.sum(dim=0, keepdim=True)
        g = nonDirectionalG_sum / lens
        
        # need to broadcast g and concat with wc
        new_shape = [nonDirectionalG.data.shape[0] // g.shape[0]] + [-1] * (len(g.shape) - 1)
        g = pack_padded_sequence(g.expand(*new_shape), lens[0,:, 0], enforce_sorted=False)
        
        wcg = torch.cat([g.data, wc], dim=-1)
        wcg = PackedSequence(wcg, *args)
        return wcg

"""
    Model from the paper
"""
class GlobalContextualDeepTransition(pl.LightningModule):
    def __init__(self, numChars, charEmbedding, numWords,
                     wordEmbedding, contextOutputUnits, contextTransitionNumber,
                        encoderUnits, decoderUnits, transitionNumber, numTags):
        super().__init__()
        self.numTags = numTags
        self.isCuda = False
        self.contextEncoder = GlobalContextualEncoder(numChars, charEmbedding, numWords,
                                                          wordEmbedding, contextOutputUnits, contextTransitionNumber)
        self.labellerInput = wordEmbedding + charEmbedding + 2 * contextOutputUnits # units in g
        self.sequenceLabeller = SequenceLabelingEncoderDecoder(self.labellerInput, encoderUnits, decoderUnits, transitionNumber, numTags)
    
    def putOnCuda(self):
        self.isCuda = True
        return self.cuda()

    def putOnCpu(self):
        self.isCuda = False
        return self.cpu()

    def init_weights(self, gloveEmbedding):
        self.apply(recursiveXavier)
        self.contextEncoder.glove.weight = torch.nn.Parameter(gloveEmbedding, requires_grad=False)
        
    def time_series_cross_entropy(self, logits, targets):
        # convert targets to onehot and smooth the labels
        alpha = 0.1
        p = 1. - alpha
        q = alpha / (self.numTags - 1)

        # convert targets to one hot using p and q
        b = targets.size(0)
        oneHot = torch.ones(b, self.numTags) * q
        oneHot[torch.arange(b), targets] =  p
        if self.isCuda:
            oneHot = oneHot.cuda()
        loss = - oneHot * F.log_softmax(logits, dim=1)
        return loss.sum(dim=1).mean()
    
    def f1_metric(self, targets, logits):
        preds = logits.argmax(dim=1)
        
    def training_step(self, batch, batch_idx):
        words, chars, charMask, targets = batch
        
        # compute the global representation and concat with word and char representations
        wcg = self.contextEncoder(words, chars, charMask)
        
        # encode concaatentated input and decode logits using sequence labeller
        logits = self.sequenceLabeller.enforced_logits(wcg, targets)
        
        # compute loss using averaging loss across different timesteps
        loss = self.time_series_cross_entropy(logits.data, targets.data)
        
        return loss