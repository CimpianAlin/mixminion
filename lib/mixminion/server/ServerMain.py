# Copyright 2002 Nick Mathewson.  See LICENSE for licensing information.
# $Id: ServerMain.py,v 1.3 2002/12/15 04:15:38 nickm Exp $

"""mixminion.ServerMain

   The main loop and related functionality for a Mixminion server.

   See the "MixminionServer" class for more information about how it
   all works. """

__all__ = [ 'MixminonServer' ]

import getopt
import os
import sys
import time

import mixminion.Config
import mixminion.Crypto
import mixminion.server.MMTPServer
import mixminion.server.Modules
import mixminion.server.PacketHandler
import mixminion.server.Queue
import mixminion.server.ServerConfig
import mixminion.server.ServerKeys

from mixminion.Common import LOG, MixError, MixFatalError, ceilDiv, \
     formatBase64, formatTime, waitForChildren

class IncomingQueue(mixminion.server.Queue.DeliveryQueue):
    """A DeliveryQueue to accept messages from incoming MMTP connections,
       process them with a packet handler, and send them into a mix pool."""

    def __init__(self, location, packetHandler):
	"""Create an IncomingQueue that stores its messages in <location>
	   and processes them through <packetHandler>."""
	mixminion.server.Queue.DeliveryQueue.__init__(self, location)
	self.packetHandler = packetHandler
	self.mixPool = None

    def connectQueues(self, mixPool):
	"""Sets the target mix queue"""
	self.mixPool = mixPool

    def queueMessage(self, msg):
	"""Add a message for delivery"""
	LOG.trace("Inserted message %s into incoming queue",
		  formatBase64(msg[:8]))
	self.queueDeliveryMessage(None, msg)

    def _deliverMessages(self, msgList):
	"Implementation of abstract method from DeliveryQueue."
	ph = self.packetHandler
	for handle, _, message, n_retries in msgList:
	    try:
		res = ph.processMessage(message)
		if res is None:
		    # Drop padding before it gets to the mix.
		    LOG.debug("Padding message %s dropped",
			      formatBase64(message[:8]))
		else:
		    LOG.debug("Processed message %s; inserting into pool",
			      formatBase64(message[:8]))
		    self.mixPool.queueObject(res)
		    self.deliverySucceeded(handle)
	    except mixminion.Crypto.CryptoError, e:
		LOG.warn("Invalid PK or misencrypted packet header: %s", e)
		self.deliveryFailed(handle)
	    except mixminion.Packet.ParseError, e:
		LOG.warn("Malformed message dropped: %s", e)
		self.deliveryFailed(handle)
	    except mixminion.server.PacketHandler.ContentError, e:
		LOG.warn("Discarding bad packet: %s", e)
		self.deliveryFailed(handle)

class MixPool:
    """Wraps a mixminion.server.Queue.*MixQueue to send messages to an exit
       queue and a delivery queue."""
    def __init__(self, queue):
	"""Create a new MixPool to wrap a given *MixQueue."""
	self.queue = queue
	self.outgoingQueue = None
	self.moduleManager = None

    def queueObject(self, obj):
	"""Insert an object into the queue."""
	self.queue.queueObject(obj)

    def count(self):
	"Return the number of messages in the queue"
	return self.queue.count()

    def connectQueues(self, outgoing, manager):
	"""Sets the queue for outgoing mixminion packets, and the
  	   module manager for deliverable messages."""
	self.outgoingQueue = outgoing
	self.moduleManager = manager

    def mix(self):
	"""Get a batch of messages, and queue them for delivery as
	   appropriate."""
	handles = self.queue.getBatch()
	LOG.debug("Mixing %s messages out of %s",
		       len(handles), self.queue.count())
	for h in handles:
	    tp, info = self.queue.getObject(h)
	    if tp == 'EXIT':
		rt, ri, app_key, tag, payload = info
		LOG.debug("  (sending message %s to exit modules)",
			  formatBase64(payload[:8]))
		self.moduleManager.queueMessage(payload, tag, rt, ri)
	    else:
		assert tp == 'QUEUE'
		ipv4, msg = info
		LOG.debug("  (sending message %s to MMTP server)",
			  formatBase64(msg[:8]))
		self.outgoingQueue.queueDeliveryMessage(ipv4, msg)
	    self.queue.removeMessage(h)

class OutgoingQueue(mixminion.server.Queue.DeliveryQueue):
    """DeliveryQueue to send messages via outgoing MMTP connections."""
    def __init__(self, location):
	"""Create a new OutgoingQueue that stores its messages in a given
 	   location."""
        mixminion.server.Queue.DeliveryQueue.__init__(self, location)
	self.server = None

    def connectQueues(self, server):
	"""Set the MMTPServer that this OutgoingQueue informs of its
	   deliverable messages."""
	self.server = server

    def _deliverMessages(self, msgList):
	"Implementation of abstract method from DeliveryQueue."
	# Map from addr -> [ (handle, msg) ... ]
	msgs = {}
	for handle, addr, message, n_retries in msgList:
	    msgs.setdefault(addr, []).append( (handle, message) )
	for addr, messages in msgs.items():
	    handles, messages = zip(*messages)
	    self.server.sendMessages(addr.ip, addr.port, addr.keyinfo,
				     list(messages), list(handles))

class _MMTPServer(mixminion.server.MMTPServer.MMTPAsyncServer):
    """Implementation of mixminion.server.MMTPServer that knows about
       delivery queues."""
    def __init__(self, config, tls):
        mixminion.server.MMTPServer.MMTPAsyncServer.__init__(self, config, tls)

    def connectQueues(self, incoming, outgoing):
        self.incomingQueue = incoming
        self.outgoingQueue = outgoing

    def onMessageReceived(self, msg):
        self.incomingQueue.queueMessage(msg)

    def onMessageSent(self, msg, handle):
        self.outgoingQueue.deliverySucceeded(handle)

    def onMessageUndeliverable(self, msg, handle, retriable):
	self.outgoingQueue.deliveryFailed(handle, retriable)

class MixminionServer:
    """Wraps and drives all the queues, and the async net server.  Handles
       all timed events."""
    ## Fields:
    # config: The ServerConfig object for this server
    # keyring: The mixminion.server.ServerKeys.ServerKeyring
    #
    # mmtpServer: Instance of mixminion.ServerMain._MMTPServer.  Receives
    #    and transmits packets from the network.  Places the packets it
    #    receives in self.incomingQueue.
    # incomingQueue: Instance of IncomingQueue.  Holds received packets
    #    before they are decoded.  Decodes packets with PacketHandler,
    #    and places them in mixPool.
    # packetHandler: Instance of PacketHandler.  Used by incomingQueue to
    #    decrypt, check, and re-pad received packets.
    # mixPool: Instance of MixPool.  Holds processed messages, and
    #    periodically decides which ones to deliver, according to some
    #    batching algorithm.
    # moduleManager: Instance of ModuleManager.  Map routing types to
    #    outging queues, and processes non-MMTP exit messages.
    # outgoingQueue: Holds messages waiting to be send via MMTP.

    def __init__(self, config):
	"""Create a new server from a ServerConfig."""
	LOG.debug("Initializing server")
	self.config = config
	self.keyring = mixminion.server.ServerKeys.ServerKeyring(config)
	if self.keyring._getLiveKey() is None:
	    LOG.info("Generating a month's worth of keys.")
	    LOG.info("(Don't count on this feature in future versions.)")
	    # We might not be able to do this, if we password-encrypt keys
	    keylife = config['Server']['PublicKeyLifetime'][2]
	    nKeys = ceilDiv(30*24*60*60, keylife)
	    self.keyring.createKeys(nKeys)

	LOG.trace("Initializing packet handler")
	self.packetHandler = self.keyring.getPacketHandler()
	LOG.trace("Initializing TLS context")
	tlsContext = self.keyring.getTLSContext()
	LOG.trace("Initializing MMTP server")
	self.mmtpServer = _MMTPServer(config, tlsContext)

	# FFFF Modulemanager should know about async so it can patch in if it
	# FFFF needs to.
	LOG.trace("Initializing delivery module")
	self.moduleManager = config.getModuleManager()
	self.moduleManager.configure(config)

	homeDir = config['Server']['Homedir']
	queueDir = os.path.join(homeDir, 'work', 'queues')

	incomingDir = os.path.join(queueDir, "incoming")
	LOG.trace("Initializing incoming queue")
	self.incomingQueue = IncomingQueue(incomingDir, self.packetHandler)
	LOG.trace("Found %d pending messages in incoming queue",
		       self.incomingQueue.count())

	mixDir = os.path.join(queueDir, "mix")
	# FFFF The choice of mix algorithm should be configurable
	LOG.trace("Initializing Mix pool")
	self.mixPool =MixPool(mixminion.server.Queue.TimedMixQueue(mixDir, 60))
	LOG.trace("Found %d pending messages in Mix pool",
		       self.mixPool.count())

	outgoingDir = os.path.join(queueDir, "outgoing")
	LOG.trace("Initializing outgoing queue")
	self.outgoingQueue = OutgoingQueue(outgoingDir)
	LOG.trace("Found %d pending messages in outgoing queue",
		       self.outgoingQueue.count())

	LOG.trace("Connecting queues")
	self.incomingQueue.connectQueues(mixPool=self.mixPool)
	self.mixPool.connectQueues(outgoing=self.outgoingQueue,
				   manager=self.moduleManager)
	self.outgoingQueue.connectQueues(server=self.mmtpServer)
	self.mmtpServer.connectQueues(incoming=self.incomingQueue,
				      outgoing=self.outgoingQueue)

    def run(self):
	"""Run the server; don't return unless we hit an exception."""
	# FFFF Use heapq to schedule events? [I don't think so; there are only
	# FFFF   two events, after all!]
	now = time.time()
	MIX_INTERVAL = 20  # FFFF Configurable!
	nextMix = now + MIX_INTERVAL
	nextShred = now + 6000
	#FFFF Unused
	#nextRotate = self.keyring.getNextKeyRotation()
	while 1:
	    LOG.trace("Next mix at %s", formatTime(nextMix,1))
	    while time.time() < nextMix:
		# Handle pending network events
		self.mmtpServer.process(1)
		# Process any new messages that have come in, placing them
		# into the mix pool.
		self.incomingQueue.sendReadyMessages()
                # Prevent child processes from turning into zombies.
                waitForChildren(1)

	    # Before we mix, we need to log the hashes to avoid replays.
	    # FFFF We need to recover on server failure.
	    self.packetHandler.syncLogs()

	    LOG.trace("Mix interval elapsed")
	    # Choose a set of outgoing messages; put them in outgoingqueue and
	    # modulemanger
	    self.mixPool.mix()
	    # Send outgoing messages
	    self.outgoingQueue.sendReadyMessages()
	    # Send exit messages
	    self.moduleManager.sendReadyMessages()

	    # Choose next mix interval
	    now = time.time()
	    nextMix = now + MIX_INTERVAL

	    if now > nextShred:
		# FFFF Configurable shred interval
		LOG.trace("Expunging deleted messages from queues")
		self.incomingQueue.cleanQueue()
		self.mixPool.queue.cleanQueue()
		self.outgoingQueue.cleanQueue()
		self.moduleManager.cleanQueues()
		nextShred = now + 6000

    def close(self):
	"""Release all resources; close all files."""
	self.packetHandler.close()

#----------------------------------------------------------------------
def usageAndExit(cmd):
    executable = sys.argv[0]
    print >>sys.stderr, "Usage: %s %s [-h] [-f configfile]" % (executable, cmd)
    sys.exit(0)

def configFromServerArgs(cmd, args):
    options, args = getopt.getopt(args, "hf:", ["help", "config="])
    if args:
	usageAndExit(cmd)
    configFile = "/etc/mixminiond.conf"
    for o,v in options:
	if o in ('-h', '--help'):
	    usageAndExit(cmd)
	if o in ('-f', '--config'):
	    configFile = v

    return readConfigFile(configFile)

def readConfigFile(configFile):
    try:
	return mixminion.server.ServerConfig.ServerConfig(fname=configFile)
    except (IOError, OSError), e:
	print >>sys.stderr, "Error reading configuration file %r:"%configFile
	print >>sys.stderr, "   ", str(e)
	sys.exit(1)
    except mixminion.Config.ConfigError, e:
	print >>sys.stderr, "Error in configuration file %r"%configFile
	print >>sys.stderr, str(e)
	sys.exit(1)
    return None #suppress pychecker warning

#----------------------------------------------------------------------
def runServer(cmd, args):
    config = configFromServerArgs(cmd, args)
    try:
	mixminion.Common.LOG.configure(config)
	LOG.debug("Configuring server")
	mixminion.Common.configureShredCommand(config)
	mixminion.Crypto.init_crypto(config)

	server = MixminionServer(config)
    except:
	LOG.fatal_exc(sys.exc_info(),"Exception while configuring server")
	print >>sys.stderr, "Shutting down because of exception"
        #XXXX print stack trace as well as logging?
	sys.exit(1)

    LOG.info("Starting server")
    try:
	server.run()
    except KeyboardInterrupt:
	pass
    except:
	LOG.fatal_exc(sys.exc_info(),"Exception while running server")
        #XXXX print stack trace as well as logging?
    LOG.info("Server shutting down")
    server.close()
    LOG.info("Server is shut down")

    sys.exit(0)

#----------------------------------------------------------------------
def runKeygen(cmd, args):
    options, args = getopt.getopt(args, "hf:n:",
                                  ["help", "config=", "keys="])
    # FFFF password-encrypted keys
    # FFFF Ability to fill gaps
    # FFFF Ability to generate keys with particular start/end intervals
    keys=1
    usage=0
    configFile = '/etc/miniond.conf'
    for opt,val in options:
	if opt in ('-h', '--help'):
	    usage=1
	elif opt in ('-f', '--config'):
	    configFile = val
	elif opt in ('-n', '--keys'):
	    try:
		keys = int(val)
	    except ValueError:
		print >>sys.stderr,("%s requires an integer" %opt)
		usage = 1
    if usage:
        print >>sys.stderr, "Usage: %s [-h] [-f configfile] [-n nKeys]"%cmd
        sys.exit(1)

    config = readConfigFile(configFile)

    LOG.setMinSeverity("INFO")
    mixminion.Crypto.init_crypto(config)
    keyring = mixminion.server.ServerKeys.ServerKeyring(config)
    print >>sys.stderr, "Creating %s keys..." % keys
    for i in xrange(keys):
	keyring.createKeys(1)
	print >> sys.stderr, ".... (%s/%s done)" % (i+1,keys)

#----------------------------------------------------------------------
def removeKeys(cmd, args):
    # FFFF Resist removing keys that have been published.
    # FFFF Generate 'suicide note' for removing identity key.
    options, args = getopt.getopt(args, "hf:", ["help", "config=",
                                                "remove-identity"])
    if args:
        print >>sys.stderr, "%s takes no arguments"%cmd
        usage = 1
        args = options = ()
    usage = 0
    removeIdentity = 0
    configFile = '/etc/miniond.conf'
    for opt,val in options:
	if opt in ('-h', '--help'):
	    usage=1
	elif opt in ('-f', '--config'):
	    configFile = val
	elif opt == '--remove-identity':
            removeIdentity = 1
    if usage:
        print >>sys.stderr, \
              "Usage: %s [-h|--help] [-f configfile] [--remove-identity]"%cmd
        sys.exit(1)

    config = readConfigFile(configFile)
    mixminion.Common.configureShredCommand(config)
    LOG.setMinSeverity("INFO")
    keyring = mixminion.server.ServerKeys.ServerKeyring(config)
    keyring.checkKeys()
    # This is impossibly far in the future.
    keyring.removeDeadKeys(now=(1L << 36))
    if removeIdentity:
        keyring.removeIdentityKey()
    LOG.info("Done removing keys")
