"""
    command to run in windows: Get-Content result.txt | python conlleval_perl.py
"""

import json
import torch
import pickle
import os
import argparse
from torch.utils.data import DataLoader
from tqdm import tqdm
import sys

from data.dataset import NERDataset
from models.networks import GlobalContextualDeepTransition
from scripts.beamsearch import BeamSearcher
import conlleval_perl as conll

def getParser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ckpt', help='pytorchlightning ckpt path', required=True)
    parser.add_argument('--type', help='testa or testb', default='testa')
    parser.add_argument('--beam', help='beamsize for decoding', default=4, type=int)
    parser.add_argument('--file', default='temp.txt', help="conlleval input")
    parser.add_argument('--cuda', default=False, action='store_true', help='use gpu')
    parser.add_argument('--root', default="data/conll03/", help='conll03 dir (data/conll03)')
    parser.add_argument('--notebook', default=False, action='store_true', help='if code on notebook')
    parser.add_argument(
        "-l", "--latex",
        default=False, action="store_true",
        help="generate LaTeX output"
    )
    parser.add_argument(
        "-r", "--raw",
        default=False, action="store_true",
        help="accept raw result tags"
    )
    parser.add_argument(
        "-d", "--delimiter",
        default=None,
        help="alternative delimiter tag (default: single space)"
    )
    parser.add_argument(
        "-o", "--oTag",
        default="O",
        help="alternative delimiter tag (default: O)"
    )
    return parser



def main(args):
    if args.notebook:
        from tqdm.notebook import tqdm
    else:
        from tqdm import tqdm
    # load dataset
    sourceName  = os.path.join(args.root, 'eng.train.src')
    targetName  = os.path.join(args.root, 'eng.train.trg')
    gloveFile   = os.path.join(args.root, 'trimmed.300d.Cased.txt')
    symbFile    = os.path.join(args.root, 'sym.glove')
    testSrc     = os.path.join(args.root, f'eng.{args.type}.src')
    testTrg     = os.path.join(args.root, f'eng.{args.type}.trg')
    predFile    = "preds.txt"
    beamSize    = args.beam
    data = NERDataset(sourceName, targetName, gloveFile, symbFile)
    data.readTestFile(testSrc, testTrg)
    loader = data.getLoader(4096, shuffle=False)


    # load model
    prevCheckpointPath = args.ckpt
    with open('config.json', 'r') as file:
        kwargs = json.load(file)
        
    model = GlobalContextualDeepTransition.load_from_checkpoint(prevCheckpointPath, **kwargs)
    model = model.eval()
    if args.cuda:
        model = model.cuda()


    # perform beamsearch
    tester = BeamSearcher(beamSize=beamSize, model=model)

    out = []
    for batch in tqdm(loader, desc=f"Beam={args.beam} ckpt={os.path.basename(args.ckpt)}"):
        if args.cuda:
            batch = (w.cuda() for w in batch)
        preds = tester(batch)
        out.extend(preds)

    tester.writePreds(out, data.tags, predFile, testSrc)
    tester.getResultFile(testSrc, predFile, testTrg, args.file)
    acc, prec, rec, f1 = conll.main(args)
    os.remove(predFile)
    os.remove(args.file)
    return acc, prec, rec, f1

if __name__ == "__main__":
    parser = getParser()
    args = parser.parse_args()
    acc, prec, rec, f1 = main(args)
    print("Accuracy: ", acc)
    print("precision:", prec)
    print("recall:   ", rec)
    print("f1-score: ", f1)