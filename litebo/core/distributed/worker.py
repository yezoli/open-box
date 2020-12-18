# This file is adapted from github repo: https://github.com/automl/HpBandSter.
import os
import time
import Pyro4
import pickle
import socket
import logging
import traceback
import threading
import configparser


class Worker(object):
	"""
	The worker is responsible for evaluating a single configuration on a single budget at a time.
	Communication to the individual workers goes via the nameserver, management of the worker-pool and job
	scheduling is done by the Dispatcher and jobs are determined by the Master. In distributed systems, each
	cluster-node runs a Worker-instance. To implement your own worker, overwrite the `__init__`- and the `compute`-method.
	The first allows to perform inital computations, e.g. loading the dataset, when the worker is started, while the
	latter is repeatedly called during the optimization and evaluates a given configuration yielding the associated loss.
	"""
	def __init__(self, run_id, logger=None, host=None, id=None, timeout=None, config_directory=None):
		"""
		
		Parameters
		----------
		run_id: anything with a __str__ method
			unique id to identify individual HpBandSter run
		nameserver: str
			hostname or IP of the nameserver
		nameserver_port: int
			port of the nameserver
		logger: logging.logger instance
			logger used for debugging output
		host: str
			hostname for this worker process
		id: anything with a __str__method
			if multiple workers are started in the same process, you MUST provide a unique id for each one of them using the `id` argument.
		timeout: int or float
			specifies the timeout a worker will wait for a new after finishing a distributed before shutting down.
			Towards the end of a long run with multiple workers, this helps to shutdown idling workers. We recommend
			a timeout that is roughly half the time it would take for the second largest budget to finish.
			The default (None) means that the worker will wait indefinitely and never shutdown on its own.
		"""
		self.run_id = run_id
		self.host = host
		self.dir = config_directory
		self.worker_id = "lite-bo.run_%s.worker.%i" % (self.run_id, os.getpid())
		if self.dir is None:
			self.dir = 'conf'
		config_path = os.path.join(self.dir, 'distrib.config')
		config = configparser.ConfigParser()
		config.read(config_path)
		name_server = dict(config.items('name_server'))
		self.nameserver = name_server['nameserver']
		self.nameserver_port = int(name_server['nameserver_port'])

		self.timeout = timeout
		self.timer = None

		if id is not None:
			self.worker_id += '.%s' % str(id)

		self.thread = None

		if logger is None:
			logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(message)s',  datefmt='%H:%M:%S')
			self.logger = logging.getLogger(self.worker_id)
		else:
			self.logger = logger

		self.logger.setLevel(logging.DEBUG)

		self.busy = False
		self.thread_cond = threading.Condition(threading.Lock())

	def run(self, background=False):
		"""
		Method to start the worker.
		
		Parameters
		----------
			background: bool
				If set to False (Default). the worker is executed in the current thread.
				If True, a new daemon thread is created that runs the worker. This is
				useful in a single worker scenario/when the compute function only simulates
				work.
		"""
		if background:
			self.worker_id += str(threading.get_ident())
			self.thread = threading.Thread(target=self._run, name='worker %s thread' % self.worker_id)
			self.thread.daemon = True
			self.thread.start()
		else:
			self._run()

	def _run(self):
		# initial ping to the dispatcher to register the worker
		try:
			with Pyro4.locateNS(host=self.nameserver, port=self.nameserver_port) as ns:
				self.logger.info('WORKER: Connected to nameserver %s' % (str(ns)))
				dispatchers = ns.list(prefix="litebo.run_%s.dispatcher" % self.run_id)
		except Pyro4.errors.NamingError:
			if self.thread is None:
				raise RuntimeError('No nameserver found. Make sure the nameserver is running at that the host (%s) and port (%s) are correct'%(self.nameserver, self.nameserver_port))
			else:
				self.logger.error('No nameserver found. Make sure the nameserver is running at that the host (%s) and port (%s) are correct'%(self.nameserver, self.nameserver_port))
				exit(1)
		except:
			raise

		self.logger.info('WORKER: register self and start listening for jobs')

		self.pyro_daemon = Pyro4.core.Daemon(host=self.host)

		with Pyro4.locateNS(self.nameserver, port=self.nameserver_port) as ns:
			uri = self.pyro_daemon.register(self, self.worker_id)
			ns.register(self.worker_id, uri)

		for dn, uri in dispatchers.items():
			try:
				self.logger.info('WORKER: found dispatcher %s'%dn)
				with Pyro4.Proxy(uri) as dispatcher_proxy:
					dispatcher_proxy.trigger_discover_worker()

			except Pyro4.errors.CommunicationError:
				self.logger.info('WORKER: Dispatcher did not respond. Waiting for one to initiate contact.')
				pass
			except:
				raise

		if len(dispatchers) == 0:
			self.logger.debug('WORKER: No dispatcher found. Waiting for one to initiate contact.')
		
		self.pyro_daemon.requestLoop()

		with Pyro4.locateNS(self.nameserver, port=self.nameserver_port) as ns:
			ns.remove(self.worker_id)

	def compute(self, config_id, config, budget, working_directory):
		""" The function you have to overload implementing your distributed.
		
		Parameters
		----------
		config_id: tuple
			a triplet of ints that uniquely identifies a configuration. the convention is
			id = (iteration, budget index, running index) with the following meaning:
			- iteration: the iteration of the optimization algorithms. E.g, for Hyperband that is one round of Successive Halving
			- budget index: the budget (of the current iteration) for which this configuration was sampled by the optimizer. This is only nonzero if the majority of the runs fail and Hyperband resamples to fill empty slots, or you use a more 'advanced' optimizer.
			- running index: this is simply an int >= 0 that sort the configs into the order they where sampled, i.e. (x,x,0) was sampled before (x,x,1).
		config: dict
			the actual configuration to be evaluated.
		budget: float
			the budget for the evaluation
		working_directory: str
			a name of a directory that is unique to this configuration. Use this to store intermediate results on lower budgets that can be reused later for a larger budget (for iterative algorithms, for example).
		Returns
		-------
		dict:
			needs to return a dictionary with two mandatory entries:
				- 'loss': a numerical value that is MINIMIZED
				- 'info': This can be pretty much any build in python type, e.g. a dict with lists as value. Due to Pyro4 handling the remote function calls, 3rd party types like numpy arrays are not supported!
		"""
		
		raise NotImplementedError("Subclass hpbandster.distributed.worker and overwrite the compute method in your worker script")

	@Pyro4.expose
	@Pyro4.oneway
	def start_computation(self, callback, id, *args, **kwargs):
		with self.thread_cond:
			while self.busy:
				self.thread_cond.wait()
			self.busy = True
		if self.timeout is not None and self.timer is not None:
			self.timer.cancel()
		self.logger.info('WORKER: start processing job %s' % str(id))
		self.logger.debug('WORKER: args: %s' % (str(args)))
		self.logger.debug('WORKER: kwargs: %s' % (str(kwargs)))
		try:
			result = {'result': self.compute(*args, config_id=id, **kwargs), 'exception': None}
		except Exception as e:
			result = {'result': None,
						'exception' : traceback.format_exc()}
		finally:
			self.logger.debug('WORKER: done with job %s, trying to register it.'%str(id))
			with self.thread_cond:
				self.busy = False
				callback.register_result(id, result)
				self.thread_cond.notify()
		self.logger.info('WORKER: registered result for job %s with dispatcher' % str(id))
		if self.timeout is not None:
			self.timer = threading.Timer(self.timeout, self.shutdown)
			self.timer.daemon = True
			self.timer.start()
		return result

	@Pyro4.expose	
	def is_busy(self):
		return self.busy
	
	@Pyro4.expose
	@Pyro4.oneway
	def shutdown(self):
		self.logger.debug('WORKER: shutting down now!')
		self.pyro_daemon.shutdown()
		if self.thread is not None:
			self.thread.join()
