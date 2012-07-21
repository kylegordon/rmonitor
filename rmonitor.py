#!/usr/bin/env python
## LineReceiver network client
## Courtesy of Kevin McDermott

#!/usr/bin/env python

from sys import stdout

from twisted.internet.protocol import ReconnectingClientFactory
from twisted.protocols.basic import LineOnlyReceiver
from twisted.internet import reactor

import ConfigParser

# Read the config file
config = ConfigParser.RawConfigParser()
config.read('rmonitor.cfg')
debug = config.getboolean('global', 'debug')

LOGFILE = config.get('global', 'logfile')
hostname = config.get('global', 'hostname')
port = config.getint('global', 'port')


#Initialize a 2D array

#height = 10
#width = 8
#competitors = [[0 for _ in xrange(height)] for _ in xrange(width)]
competitors = []
# competitors = [['Transponder', 'Registration', 'First Name', 'Second name', 'Position', 'Last lap time', 'Best lap time', 'Best lap']]

class RaceTimeReceiver(LineOnlyReceiver):
    delimiter = "\n"

    def lineReceived(self, data):
    	competitor = []
        # Process the line you're receiving here...
        ## print "Line received %s" % data
        # Strip the carriage return and split it on commas
        data = data.strip("\r")
        data = data.split(',')
        #print data
        # Decide what command has been issued. See AMB documentation
        command = data[0]
        #if command == "$F":
                # print "Heartbeat @ " + data[3]
        if command == "$A":
                # $A always comes before $COMP, and $A carries the transponder number
                # print "Competitor information : " + str(data)
		competitor.append(data[3]) # Transponder
		competitor.append(data[1]) # Number
		competitor.append(data[4]) # first name
		competitor.append(data[5]) # Second name
		# print competitor
		competitors.append(competitor)
		print competitors
        elif command == "$COMP":
                print "Competitor information : " + str(data)
        elif command == "$B":
                print "Run information"
        elif command == "$C":
                print "Class information : " + str(data)
        elif command == "$E":
                print "Setting information : " + str(data)
        elif command == "$G":
                print "Race information : " + str(data)
        #elif command == "$H":
                # print "Practice/Qualifying information : "  + str(data)
        elif command == "$I":
                print "Init record"
        elif command == "$J":
		# Registration, lap time, Total time
                print "Passing information : "  + str(data)
		
	## Append to the big array of doom here
	# Check that competitor isn't empty
	#if competitor.length:
	#	competitors.append(competitor)
	
	## Call something to do something

class RaceTimeClientFactory(ReconnectingClientFactory):

    protocol = RaceTimeReceiver

    def buildProtocol(self, addr):
        self.resetDelay()
        return ReconnectingClientFactory.buildProtocol(self, addr)

    def clientConnectionLost(self, connector, reason):
        print "Lost connection.  Reason:", reason
        ReconnectingClientFactory.clientConnectionLost(self, connector, reason)

    def clientConnectionFailed(self, connector, reason):
        print "Connection failed. Reason:", reason
        ReconnectingClientFactory.clientConnectionFailed(self, connector,
                                                         reason)
if __name__ == "__main__":
    factory = RaceTimeClientFactory()
    reactor.connectTCP(hostname, port, factory)
    reactor.run()
