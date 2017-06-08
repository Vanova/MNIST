import matplotlib.pyplot as plt

# plt.xlim(0,1000)
plt.ylim(40, 100)

legend_entries = []
for k in [1, 10]:
	f = open('Results/disc/SF/training_sf_cmr_' + str(k) + '_100_0.0001.txt','r').read().splitlines()
	plt.plot([float(line.split(',')[2]) for idx, line in enumerate(f) if idx % 600 == 0])
	legend_entries += ['SF_' + str(k)]

	f = open('Results/disc/synthetic_gradients/training_sg_inp_act_out_lin_cmr_' + str(k) + '_100_0.0001.txt','r').read().splitlines()
	plt.plot([float(line.split(',')[2]) for idx, line in enumerate(f) if idx % 600 == 0])
	legend_entries += ['SGlin_' + str(k)]

	f = open('Results/disc/synthetic_gradients/training_sg_inp_act_out_deep_cmr_' + str(k) + '_100_0.0001.txt','r').read().splitlines()
	plt.plot([float(line.split(',')[2]) for idx, line in enumerate(f) if idx % 600 == 0])
	legend_entries += ['SGdeep_' + str(k)]

plt.grid()
plt.legend(legend_entries)

# plt.savefig('Results/disc/training_plots', ext='png', close=False, verbose=True, dpi=350, bbox_inches='tight', pad_inches=0)
plt.show()
plt.close()
