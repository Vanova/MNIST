import sys

import numpy as np
from read_mnist import read, show
import theano
import theano.tensor as T
from utils import init_weights, _concat
import argparse

from adam import adam
from sgd import SGD

from collections import OrderedDict
import time

'''
Compares different gradients in terms of bias, variance and directionality. Generates 250 latent samples per example with 250-sample REINFORCE considered as the true gradient.
'''
# ---------------------------------------------------------------------------------------------------------------------------------------------------------

parser = argparse.ArgumentParser()
# training configuration
parser.add_argument('-r', '--repeat', type=int, default=250, 
					help='Determines the number of samples per training example for SF estimator')

# hyperparameters
parser.add_argument('-a', '--learning_rate', type=float, default=0.0001, help='Learning rate')
parser.add_argument('-b', '--batch_size', type=int, default=100, help='Size of the minibatch used for training')
parser.add_argument('-x', '--sg_type',type=str, default='lin_deep', 
					help='Type of synthetic gradient subnetwork: linear (lin) or a two-layer nn (deep) or both (lin_deep)')
parser.add_argument('-j', '--dropout_prob', type=float, default=0.5, help='Probability with which neuron is dropped')
parser.add_argument('-z', '--bn_type', type=int, default=1,
					help='0: BN->Matrix Multiplication->Nonlinearity, 1: Matrix Multiplication->BN->Nonlinearity')

# termination of training
parser.add_argument('-t', '--term_condition', type=str, default='epochs', 
					help='Training terminates either when number of epochs are completed (epochs) or when minimum cost is achieved for a batch (mincost)')
parser.add_argument('-n', '--num_epochs', type=int, default=1000, 
					help='Number of epochs, to be specified when termination condition is epochs')
parser.add_argument('-d', '--min_cost', type=float, default=55.0, 
					help='Minimum cost to be achieved for a minibatch, to be specified when termination condition is mincost')

# while testing
parser.add_argument('-l', '--load', type=str, default=None, help='Path to weights')

# miscellaneous
parser.add_argument('-q', '--random_seed', type=int, default=42, help='Seed to initialize random streams')
parser.add_argument('-y', '--base_code', type=str, default='', help='Unique identifier for the files generated by the process')
args = parser.parse_args()

# random seed and initialization of stream
if "gpu" in theano.config.device:
	srng = theano.sandbox.rng_mrg.MRG_RandomStreams(seed=args.random_seed)
else:
	srng = T.shared_randomstreams.RandomStreams(seed=args.random_seed)

code_name = args.base_code + '_' + str(args.repeat)
# ---------------------------------------------------------------------------------------------------------------------------------------------------------

# converts images into binary images for simplicity
def binarize_img(img):
	return np.asarray(img >= 100, dtype=np.int8)

# split into two images
def split_img(img):
	# images are flattened into a vector: just need to split into half
	veclen = len(img)
	return (img[:veclen/2], img[veclen/2:])

def param_init_fflayer(params, prefix, nin, nout, zero_init=False, batchnorm=False):
	'''
	Initializes weights for a feedforward layer
	'''
	global args
	if zero_init:
		params[_concat(prefix, 'W')] = np.zeros((nin, nout)).astype('float32')
	else:
		params[_concat(prefix, 'W')] = init_weights(nin, nout, type_init='ortho')
	
	params[_concat(prefix, 'b')] = np.zeros((nout,)).astype('float32')
	
	if batchnorm:
		if args.bn_type == 0:
			dim = nin
		else:
			dim = nout
		params[_concat(prefix, 'g')] = np.ones((dim,), dtype=np.float32)
		params[_concat(prefix, 'be')] = np.zeros((dim,)).astype('float32')
		params[_concat(prefix, 'rm')] = np.zeros((1, dim)).astype('float32')
		params[_concat(prefix, 'rv')] = np.ones((1, dim), dtype=np.float32)
	
	return params

def fflayer(tparams, state_below, prefix, nonlin='tanh', batchnorm=None, dropout=None):
	'''
	A feedforward layer
	Note: None means dropout/batch normalization is not used.
	Use 'train' or 'test' options.
	'''
	global srng, args

	# apply batchnormalization on the input
	if args.bn_type == 0:
		inp = state_below
	else:
		inp = T.dot(state_below, tparams[_concat(prefix, 'W')]) + tparams[_concat(prefix, 'b')]

	if batchnorm == 'train':
		axes = (0,)
		mean = inp.mean(axes, keepdims=True)
		var = inp.var(axes, keepdims=True)
		invstd = T.inv(T.sqrt(var + 1e-4))
		inp = (inp - mean) * tparams[_concat(prefix, 'g')] * invstd + tparams[_concat(prefix, 'be')]
		
		running_average_factor = 0.1	
		m = T.cast(T.prod(inp.shape) / T.prod(mean.shape), 'float32')
		tparams[_concat(prefix, 'rm')] = tparams[_concat(prefix, 'rm')] * (1 - running_average_factor) + mean * running_average_factor
		tparams[_concat(prefix, 'rv')] = tparams[_concat(prefix, 'rv')] * (1 - running_average_factor) + (m / (m - 1)) * var * running_average_factor
		
	elif batchnorm == 'test':
		inp = (inp - tparams[_concat(prefix, 'rm')].flatten()) * tparams[_concat(prefix, 'g')] / T.sqrt(tparams[_concat(prefix, 'rv')].flatten() + 1e-4) + tparams[_concat(prefix, 'be')]
	
	if args.bn_type == 0:
		preact = T.dot(inp, tparams[_concat(prefix, 'W')]) + tparams[_concat(prefix, 'b')]
	else:
		preact = inp

	# dropout is carried out with fixed probability
	if dropout == 'train':
		dropmask = srng.binomial(n=1, p=1. - args.dropout_prob, size=preact.shape, dtype=theano.config.floatX)
		preact *= dropmask
	
	elif dropout == 'test':
		preact *= 1. - args.dropout_prob

	if nonlin == None:
		return preact
	elif nonlin == 'tanh':
		return T.tanh(preact)
	elif nonlin == 'sigmoid':
		return T.nnet.nnet.sigmoid(preact)
	elif nonlin == 'softplus':
		return T.nnet.nnet.softplus(preact)
	elif nonlin == 'relu':
		return T.nnet.nnet.relu(preact)

def param_init_sgmod(params, prefix, units, zero_init=True):
	'''
	Initialization for synthetic gradient subnetwork
	'''
	global args
	
	# conditioned on the whole image, on the activation produced by encoder input and the backpropagated gradients for latent samples.
	inp_list = [14*28, 14*28, units, units, units]
	inp_size = 0
	for i in range(5):
		inp_size += inp_list[i]

	if not zero_init:
		if args.sg_type == 'lin':
			params[_concat(prefix, 'W')] = init_weights(inp_size, units, type_init='ortho')
			params[_concat(prefix, 'b')] = np.zeros((units,)).astype('float32')

	else:
		if args.sg_type == 'lin' or args.sg_type == 'lin_deep':
			params[_concat(prefix, 'W')] = np.zeros((inp_size, units)).astype('float32')
			params[_concat(prefix, 'b')] = np.zeros((units,)).astype('float32')

		if args.sg_type == 'deep' or args.sg_type == 'lin_deep':
			params = param_init_fflayer(params, _concat(prefix, 'I'), inp_size, 1024, batchnorm=True)
			params = param_init_fflayer(params, _concat(prefix, 'H'), 1024, 1024, batchnorm=True)
			if args.bn_type == 0:
				params = param_init_fflayer(params, _concat(prefix, 'o'), 1024, units, zero_init=True, batchnorm=True)
			else:
				params = param_init_fflayer(params, _concat(prefix, 'o'), 1024, units, zero_init=True, batchnorm=False)

	return params

def synth_grad(tparams, prefix, inp, mode='Train'):
	'''
	Synthetic gradients
	'''
	global args
	if args.sg_type == 'lin':
		return T.dot(inp, tparams[_concat(prefix, 'W')]) + tparams[_concat(prefix, 'b')]
	
	elif args.sg_type == 'deep' or args.sg_type == 'lin_deep':
		outi = fflayer(tparams, inp, _concat(prefix, 'I'), nonlin='relu', batchnorm='train', dropout=None)
		outh = fflayer(tparams, outi, _concat(prefix,'H'), nonlin='relu', batchnorm='train', dropout=None)
		
		# depending on the bn type being used, bn is used/not used in the layer
		if args.bn_type == 0:
			bn_last = 'train'
		else:
			bn_last = None
		
		if args.sg_type == 'deep':
			return fflayer(tparams, outh + outi, _concat(prefix, 'o'), batchnorm=bn_last, nonlin=None)
		elif args.sg_type == 'lin_deep':
			return T.dot(inp, tparams[_concat(prefix, 'W')]) + tparams[_concat(prefix, 'b')] + fflayer(tparams, outh + outi, _concat(prefix, 'o'), batchnorm=bn_last, nonlin=None)
# ---------------------------------------------------------------------------------------------------------------------------------------------------------

print "Creating partial images"
# collect training data and converts image into binary and does row major flattening
trc = np.asarray([binarize_img(img).flatten() for lbl, img in read(dataset='training', path ='MNIST/')], dtype=np.float32)

# collect test data and converts image into binary and does row major flattening
tec = np.asarray([binarize_img(img).flatten() for lbl, img in read(dataset='testing', path = 'MNIST/')], dtype=np.float32)

# split images
trp = [split_img(img) for img in trc]
tep = [split_img(img) for img in tec]

print "Initializing parameters"
# parameter initializations
ff_e = 'ff_enc'
ff_d = 'ff_dec'
sg = 'sg'
latent_dim = 50


params = OrderedDict()

# encoder
params = param_init_fflayer(params, _concat(ff_e, 'i'), 14*28, 200, batchnorm=True)
params = param_init_fflayer(params, _concat(ff_e, 'h'), 200, 100, batchnorm=True)

# latent
if args.bn_type == 0:
	params = param_init_fflayer(params, _concat(ff_e, 'bern'), 100, latent_dim, batchnorm=True)
else:
	params = param_init_fflayer(params, _concat(ff_e, 'bern'), 100, latent_dim, batchnorm=False)

# synthetic gradient module for the last encoder layer
params = param_init_sgmod(params, _concat(sg, 'r'), latent_dim)

# loss prediction neural network, conditioned on input and output (in this case the whole image). Acts as the baseline
params = param_init_fflayer(params, 'loss_pred', 28*28, 1)

# decoder parameters
params = param_init_fflayer(params, _concat(ff_d, 'n'), latent_dim, 100)
params = param_init_fflayer(params, _concat(ff_d, 'h'), 100, 200, batchnorm=True)
if args.bn_type == 0 :
	params = param_init_fflayer(params, _concat(ff_d, 'o'), 200, 14*28, batchnorm=True)
else:
	params = param_init_fflayer(params, _concat(ff_d, 'o'), 200, 14*28, batchnorm=False)

# restore from saved weights
if args.load is not None:
	lparams = np.load(args.load)

	for key, val in lparams.iteritems():
		params[key] = val

tparams = OrderedDict()
for key, val in params.iteritems():
	tparams[key] = theano.shared(val, name=key)
# ---------------------------------------------------------------------------------------------------------------------------------------------------------

# Training Graph
print "Constructing graph for training"
# create shared variables for dataset for easier access
top = np.asarray([splitimg[0] for splitimg in trp], dtype=np.float32)
bot = np.asarray([splitimg[1] for splitimg in trp], dtype=np.float32)

train = theano.shared(top, name='train')
train_gt = theano.shared(bot, name='train_gt')

# pass a batch of indices while training
img_ids = T.vector('ids', dtype='int64')
img = train[img_ids, :]
img_r = T.extra_ops.repeat(img, args.repeat, axis=0)
gt_unrepeated = train_gt[img_ids, :]
gt = T.extra_ops.repeat(gt_unrepeated, args.repeat, axis=0)

# inputs for synthetic gradient networks, provide the top half of the image as well
target_gradients = T.matrix('tg', dtype='float32')
activation = T.matrix('sg_input_probs', dtype='float32')
latent_gradients = T.matrix('sg_input_latgrads', dtype='float32')
samples = T.matrix('sg_input_samples', dtype='float32')

# encoding
out1 = fflayer(tparams, img, _concat(ff_e, 'i'), batchnorm='train')
out2 = fflayer(tparams, out1, _concat(ff_e,'h'), batchnorm='train')

if args.bn_type == 0:
	latent_probs = fflayer(tparams, out2, _concat(ff_e, 'bern'), nonlin='sigmoid', batchnorm='train')
else:
	latent_probs = fflayer(tparams, out2, _concat(ff_e, 'bern'), nonlin='sigmoid', batchnorm=None)

latent_probs_r = T.extra_ops.repeat(latent_probs, args.repeat, axis=0)

# sample a bernoulli distribution, which a binomial of 1 iteration
latent_samples_uncorrected = srng.binomial(size=latent_probs_r.shape, n=1, p=latent_probs_r, dtype=theano.config.floatX)

# for stop gradients trick
dummy = latent_samples_uncorrected - latent_probs_r
latent_samples = latent_probs_r + dummy

# decoding
outz = fflayer(tparams, latent_samples, _concat(ff_d, 'n'))
outh = fflayer(tparams, outz, _concat(ff_d, 'h'), batchnorm='train')
if args.bn_type == 0:
	probs = fflayer(tparams, outh, _concat(ff_d, 'o'), nonlin='sigmoid', batchnorm='train')
else:
	probs = fflayer(tparams, outh, _concat(ff_d, 'o'), nonlin='sigmoid', batchnorm=None)
# ---------------------------------------------------------------------------------------------------------------------------------------------------------

# having large number of latent samples is necessary: multi-sample REINFORCE essentially gives the true gradients at the tradeoff of speed.
reconstruction_loss = T.nnet.binary_crossentropy(probs, gt).mean(axis=1)

# separate parameters for encoder and decoder
param_dec = [val for key, val in tparams.iteritems() if ('ff_dec' in key) and ('rm' not in key and 'rv' not in key)]
param_enc = [val for key, val in tparams.iteritems() if ('ff_enc' in key) and ('rm' not in key and 'rv' not in key)]

# decoder gradients
cost_decoder = T.mean(reconstruction_loss)
grads_decoder = T.grad(cost_decoder, wrt=param_dec)

# REINFORCE gradients: conditional mean is subtracted from the reconstruction loss to lower variance further
baseline = T.extra_ops.repeat(fflayer(tparams, T.concatenate([img, train_gt[img_ids, :]], axis=1), 'loss_pred', nonlin='relu'), args.repeat, axis=0)
cost_encoder = T.mean((reconstruction_loss - baseline.T) * T.switch(latent_samples, T.log(latent_probs_r), T.log(1. - latent_probs_r)).sum(axis=1))
consider_constant = [reconstruction_loss, latent_samples, baseline]
grads_encoder = T.grad(cost_encoder, wrt=param_enc + [latent_probs_r, latent_probs], consider_constant=consider_constant)

# true gradient is scaled up 100 times example wise and 1-sample reinforce is scaled up by 250*100 to account for the "mean" costs
true_gradient = grads_encoder[-1] # * args.batch_size
true_gradient_norm = (true_gradient ** 2).sum() # / args.batch_size
reinforce_1 = args.repeat * grads_encoder[-2] # * args.batch_size
grads_encoder = grads_encoder[:-2]

# optimizing the loss predictor for conditional mean baseline
cost_pred = 0.5 * ((reconstruction_loss - baseline.T) ** 2).sum()
params_loss_predictor = [val for key, val in tparams.iteritems() if 'loss_pred' in key]
grads_plp = T.grad(cost_pred, wrt=params_loss_predictor, consider_constant=[reconstruction_loss])

grads = grads_encoder + grads_plp + grads_decoder

# computation of different gradients, bias and variances: we have already computed true gradient example wise above
temp = T.extra_ops.repeat(true_gradient, args.repeat, axis=0)

# bias-variance of 1-sample reinforce: expected value for the gradient is the true gradient itself: bias should approximately be zero
bias2_reinforce = ((reinforce_1.reshape((args.batch_size, args.repeat, latent_dim)).sum(axis=1) / args.repeat - true_gradient) ** 2).sum() # / args.batch_size
var_reinforce = ((reinforce_1 - temp) ** 2).sum() / (args.repeat) # * args.batch_size)
r_samedir = T.cast((reinforce_1 * temp).sum(axis=1) > 0, 'float32').sum() / (args.batch_size * args.repeat)

# bias-variance decomposition of straight through estimator
st = args.repeat * T.grad(cost_decoder, wrt=latent_probs_r, consider_constant=[dummy]) # * args.batch_size 
ez_st = st.reshape((args.batch_size, args.repeat, latent_dim)).sum(axis=1) / args.repeat
ez_st_norm = (ez_st ** 2).sum() # / args.batch_size
bias2_st = ((ez_st - true_gradient) ** 2).sum() # / args.batch_size
var_st = ((st - T.extra_ops.repeat(ez_st, args.repeat, axis=0)) ** 2).sum() / (args.repeat) # * args.batch_size)
st_samedir = T.cast((st * temp).sum(axis=1) > 0, 'float32').sum() / (args.batch_size * args.repeat)

# bias-variance decomposition of synthetic gradients
param_sg = [val for key, val in tparams.iteritems() if ('sg' in key) and ('rm' not in key and 'rv' not in key)]
gradz = args.repeat * T.grad(cost_decoder, wrt=latent_samples) # * args.batch_size

# the multiplications by repeat/batch_size are not carried out because synthetic gradients would produce the same gradients even if one sample/example was given.
var_list = [img_r, gt, latent_probs_r, gradz, latent_samples]
sg_r = synth_grad(tparams, _concat(sg, 'r'), T.concatenate(var_list, axis=1), mode='test')
ez_sg = sg_r.reshape((args.batch_size, args.repeat, latent_dim)).sum(axis=1) / args.repeat
ez_sg_norm = (ez_sg ** 2).sum() # / args.batch_size

bias2_sg = ((ez_sg - true_gradient) ** 2).sum() # / args.batch_size
var_sg = ((sg_r - T.extra_ops.repeat(ez_sg, args.repeat, axis=0)) ** 2).sum() / (args.repeat) # * args.batch_size)
sg_samedir = T.cast((sg_r * temp).sum(axis=1) > 0, 'float32').sum() / (args.batch_size * args.repeat)

# optimizing the synthetic gradient subnetwork
loss_sg = T.mean((target_gradients - synth_grad(tparams, _concat(sg, 'r'), T.concatenate([img_r, gt, activation, latent_gradients, samples], axis=1)).reshape((args.batch_size, args.repeat, latent_dim)).sum(axis=1) / args.repeat) ** 2)
grads_sg = T.grad(loss_sg, wrt=param_sg)

# ------------------------------------------------------------------ General training routine ----------------------------------------------------------------------------------

# learning rate
lr = T.scalar('lr', dtype='float32')

inps_net = [img_ids]
'''Do not forget to divide by batch size, when you return from this pretense'''
outs = [cost_decoder, true_gradient, latent_probs_r, gradz, latent_samples, true_gradient_norm, bias2_reinforce, var_reinforce, r_samedir, ez_st_norm, bias2_st, var_st, st_samedir, ez_sg_norm, bias2_sg, var_sg, sg_samedir]
inps_sg = inps_net + [target_gradients, activation, latent_gradients, samples]
tparams_net = OrderedDict()
tparams_sg = OrderedDict()

for key, val in tparams.iteritems():
	print key
	# running means and running variances should not be included in the list for which gradients are computed
	if 'rm' in key or 'rv' in key:
		continue
	elif 'sg' in key:
		tparams_sg[key] = val
	else:
		tparams_net[key] = val

print "Setting up optimizers"
f_grad_shared, f_update = adam(lr, tparams_net, grads, inps_net, outs)
# f_grad_shared_sg, f_update_sg = adam(lr, tparams_sg, grads_sg, inps_sg, loss_sg)

# sgd with momentum updates
sgd = SGD(lr=args.learning_rate)
f_update_sg = theano.function(inps_sg, loss_sg, updates=sgd.get_grad_updates(loss_sg, param_sg), on_unused_input='ignore', profile=False)

print "Training"
cost_report = open('./Results/disc/SF/gradcomp_' + code_name + '_' + str(args.batch_size) + '_' + str(args.learning_rate) + '.txt', 'w')
id_order = range(len(trc))

iters = 0
min_cost = 100000.0
epoch = 0
condition = False

while condition == False:
	print "Epoch " + str(epoch + 1),
	np.random.shuffle(id_order)
	epoch_cost = 0.
	epoch_diff = 0.
	epoch_start = time.time()
	
	for batch_id in range(len(trc)/args.batch_size):
		batch_start = time.time()

		idlist = id_order[batch_id*args.batch_size:(batch_id+1)*args.batch_size]
		
		cost, t, lpc, gradz, ls, tgn, br, vr, sr, sn, bs, vs, ss, sgn, bsg, vsg, ssg = f_grad_shared(idlist)	
		min_cost = min(min_cost, cost)
		f_update(args.learning_rate)
		
		cost_sg = f_update_sg(idlist, t, lpc, gradz, ls)
		# f_update_sg(args.learning_rate)
		
		epoch_cost += cost
		# epoch, batch id, main networks cost, norm of true gradient, bias-reinforce, variance-reinforce, reinforce half-space correlation, straight-through squared norm, bias-straight through, variance-straight through, reinforce half-space correlation, synthetic gradient squared norm, bias-synthetic gradient, variance synthetic gradient, half-space correlation, time of computation
		cost_report.write(str(epoch) + ',' + str(batch_id) + ',' + str(cost)  + ',' + str(tgn) + ',' + str(br) + ',' + str(vr) + ',' + str(sr) + ',' + str(sn) + ',' + str(bs) + ',' + str(vs) + ',' + str(ss) + ',' + str(sgn) + ',' + str(bsg) + ',' + str(vsg) + ',' + str(ssg) + ',' + str(time.time() - batch_start) + '\n')

	print ": Cost " + str(epoch_cost) + " : Time " + str(time.time() - epoch_start)

	epoch += 1
	if args.term_condition == 'mincost' and min_cost < args.min_cost:
		condition = True
	elif args.term_condition == 'epochs' and epoch >= args.num_epochs:
		condition = True