#!/bin/bash

mkdir MNIST/
cd MNIST/
wget "http://yann.lecun.com/exdb/mnist/train-images-idx3-ubyte.gz"
wget "http://yann.lecun.com/exdb/mnist/train-labels-idx1-ubyte.gz"
wget "http://yann.lecun.com/exdb/mnist/t10k-images-idx3-ubyte.gz"
wget "http://yann.lecun.com/exdb/mnist/t10k-labels-idx1-ubyte.gz"

gunzip train-images-idx3-ubyte.gz
gunzip train-labels-idx1-ubyte.gz
gunzip t10k-images-idx3-ubyte.gz
gunzip t10k-labels-idx1-ubyte.gz

cd ../
mkdir Results_VAE/
mkdir Results_VAE/PD

mkdir Results/
mkdir Results/cont
mkdir Results/disc
mkdir Results/cont/PD/
mkdir Results/cont/SF/
mkdir Results/disc/SF/
mkdir Results/disc/PD/