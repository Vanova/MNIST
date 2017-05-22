import sys

import numpy as np
from read_mnist import read
import theano
import theano.tensor as T
from utils import save_obj, load_obj, init_weights, _concat
from adam import adam

from collections import OrderedDict
import time

'''
Script Arguments
No arguments = train from random initialization
1: Train = 0, Test = 1
2: Address for weights
'''

seed = 42
learning_rate = 0.001
EPOCHS = 100
batch_size = 100

# converts images into binary images for simplicity
def binarize_img(img):
	return np.asarray(img >= 100, dtype=np.int8)

# split into two images
def split_img(img):
	# images are flattened into a vector: just need to split into half
	veclen = len(img)
	return (img[:veclen/2], img[veclen/2:])

def param_init_fflayer(params, prefix, nin, nout):
	'''
	Initializes weights for a feedforward layer
	'''
	params[_concat(prefix,'W')] = init_weights(nin, nout, type_init='ortho')
	params[_concat(prefix,'b')] = np.zeros((nout,)).astype('float32')

	return params

def fflayer(tparams, state_below, prefix, nonlin='tanh'):
	'''
	A feedforward layer
	'''
	if nonlin == None:
		return T.dot(state_below, tparams[_concat(prefix, 'W')]) + tparams[_concat(prefix, 'b')]
	elif nonlin == 'tanh':
		return T.tanh(T.dot(state_below, tparams[_concat(prefix, 'W')]) + tparams[_concat(prefix, 'b')])
	elif nonlin == 'sigmoid':
		return T.nnet.nnet.sigmoid(T.dot(state_below, tparams[_concat(prefix, 'W')]) + tparams[_concat(prefix, 'b')])

print "Creating partial images"
# collect training data and converts image into binary and does row major flattening
trc = np.asarray([binarize_img(img).flatten() for lbl, img in read(dataset='training', path ='MNIST/')], dtype=np.float32)

# collect test data and converts image into binary and does row major flattening
tec = np.asarray([binarize_img(img).flatten() for lbl, img in read(dataset='testing', path = 'MNIST/')], dtype=np.float32)

# split images
trp = np.asarray([split_img(img)[0] for img in trc], dtype=np.float32)
tep = np.asarray([split_img(img)[0] for img in tec], dtype=np.float32)

print "Initializing parameters"
# parameter initializations
ff_e = 'ff_enc'
ff_d = 'ff_dec'
latent_dim = 50

# no address provided for weights
if len(sys.argv) < 3:
	params = OrderedDict()

	# encoder
	params = param_init_fflayer(params, _concat(ff_e, 'c'), 28*28, 300) 
	params = param_init_fflayer(params, _concat(ff_e, 'p'), 14*28, 200)

	# common hidden layer
	params = param_init_fflayer(params, _concat(ff_e, 'h'), 300+200, 250)

	# latent distribution parameters
	params = param_init_fflayer(params, 'mu', 250, latent_dim)
	params = param_init_fflayer(params, 'sigma', 250, latent_dim)

	# decoder parameters
	params = param_init_fflayer(params, _concat(ff_d, 'n'), latent_dim, 100)
	params = param_init_fflayer(params, _concat(ff_d, 'p'), 14*28, 200)
	params = param_init_fflayer(params, _concat(ff_d, 'h'), 200+100, 500)
	params = param_init_fflayer(params, _concat(ff_d, 'o'), 500, 28*28)

else:
	# restore from saved weights
	params = np.load(sys.argv[3])

tparams = OrderedDict()
for key, val in params.iteritems():
	tparams[key] = theano.shared(val, name=key)

# Training graph
if len(sys.argv) < 2 or int(sys.argv[2]) == 0:
	print "Constructing graph for training"
	# create shared variables for dataset for easier access
	trainC = theano.shared(trc, name='train')
	trainP = theano.shared(trp, name='partial')

	# pass a batch of indices while training
	img_ids = T.vector('ids', dtype='int64')
	img = trainC[img_ids,:]
	partial_img = trainP[img_ids, :]

	outc = fflayer(tparams, img, _concat(ff_e, 'c'))
	outp = fflayer(tparams, partial_img, _concat(ff_e, 'p'))

	combine = T.concatenate([outc, outp], axis=1)
	out = fflayer(tparams, combine, _concat(ff_e, 'h'))

	mu = fflayer(tparams, out, 'mu', nonlin=None)
	sd = fflayer(tparams, out, 'sigma', nonlin=None)

	if "gpu" in theano.config.device:
		srng = theano.sandbox.cuda.rng_curand.CURAND_RandomStreams(seed=seed)
	else:
		srng = T.shared_randomstreams.RandomStreams(seed=seed)

	# sampling from zero mean normal distribution
	eps = srng.normal(mu.shape)
	latent_samples = mu + sd * eps

	outz = fflayer(tparams, latent_samples, _concat(ff_d, 'n'))
	outp_dec = fflayer(tparams, partial_img, _concat(ff_d, 'p'))

	combine_dec = T.concatenate([outz, outp_dec], axis=1)

	outh = fflayer(tparams, combine_dec, _concat(ff_d, 'h'))
	probs = fflayer(tparams, outh, _concat(ff_d, 'o'), nonlin='sigmoid')

	# KL Divergence loss between assumed posterior and prior
	KL = 0.5 * (1 + T.log(sd ** 2) - mu ** 2 - sd ** 2).sum(axis=1)
	# Reconstruction loss
	RL = -T.nnet.binary_crossentropy(probs, img).sum(axis=1)
	cost = T.mean(KL + RL)

	print "Computing gradients"
	param_list = [val for key, val in tparams.iteritems()]
	grads = T.grad(cost, wrt=param_list)

	# learning rate
	lr = T.scalar('lr', dtype='float32')

	inps = [img_ids]

	print "Setting up optimizer"
	f_grad_shared, f_update = adam(lr, tparams, grads, inps, cost)

	print "Training"
	id_order = [i for i in range(len(trc))]
	for epoch in range(EPOCHS):
		print "Epoch " + str(epoch + 1)

		np.random.shuffle(id_order)

		for batch_id in range(len(trc)/batch_size):
			idlist = id_order[batch_id*batch_size:(batch_id+1)*batch_size]
			cost = f_grad_shared(idlist)
			f_update(learning_rate)

		
# Test graph
else:
	pass