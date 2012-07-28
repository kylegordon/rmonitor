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
old = [0,0,0,0,0,0,0,0]

def search_nested(mylist, val):
    """ 
    This function will search each cell of mylist for val and
    if found will return the entire row
    """
    #loops i from 0 to the length of mylist (num of rows)
    for i in range(len(mylist)):
        #loops j from 0 to num of cols in each row
        for j in range(len(mylist[i])):
                #print i, j # my own debugging commented out
                #compare each cell to search value
                if mylist[i][j] == val:
                        #if found, return entire row that the value was found in and the row number
                        return mylist[i], i
    #if value not found, return a string instead
    return str(val) + ' not found'


class RaceTimeReceiver(LineOnlyReceiver):
    delimiter = "\n"

    def lineReceived(self, data):
	# Blank/create the individual array
	# competitor = ['Transponder', 'Registration', 'First Name', 'Second name', 'Position', 'Last lap time', 'Best lap time', 'Best lap']
    	competitor = [0,0,0,0,0,0,0,0]
	
	global old 

	resultindex = ''

        # Process the line you're receiving here...
        ## print "Line received %s" % data
        # Strip the carriage return and split it on commas
        data = data.strip("\r")
        data = data.split(',')
	data = map(lambda foo: foo.replace('"', ''), data)
        #print data
        # Decide what command has been issued. See AMB documentation
        command = data[0]
        #if command == "$F":
                # print "Heartbeat @ " + data[3]
        if command == "$A":
                # $A always comes before $COMP, and $A carries the transponder number
                # print "Competitor information : " + str(data)
                competitor[0] = data[3] # Transponder
                competitor[1] = data[1] # Number
                competitor[2] = data[4] # first name
                competitor[3] = data[5] # Second name
		competitors.append(competitor)
        elif command == "$COMP":
                print "Competitor information : " + str(data)
        elif command == "$B":
                print "Run information"
        elif command == "$C":
                print "Class information : " + str(data)
        elif command == "$E":
                print "Setting information : " + str(data)
        elif command == "$G":
		# There's a dump of $Gs upon connecting. 
                print "Race positions information : " + str(data)
                # Find the competitor by entry number
                result = search_nested(competitors, data[2])
		if not "not found" in result:
			resultdata = result[0]
                	resultindex = result[1]
        	        competitors[resultindex][4] = data[1] # Position
		else: print "Couldn't find competitor to update"

        #elif command == "$H":
                # print "Practice/Qualifying information : "  + str(data)
        elif command == "$I":
                print "Init record"
        elif command == "$J":
		# Registration, lap time, Total time
                print "Passing information : "  + str(data)
		# Find the competitor by entry number
		result = search_nested(competitors, data[1])
		resultdata = result[0]
		resultindex = result[1]
		## FIXME Don't update if lap time is 00:00:00.000 (first lap)
		# print "Updating passing : " + str(resultdata) + " at index " + str(resultindex)
		competitors[resultindex][5] = data[2] # Last lap time

	## Grab the old record. If it's for the same competitor it's been a lap or posiiton update. Tweet appropriately
	if resultindex and (competitors[resultindex] != old): 
		# print competitors[resultindex]
		print "Entrant " + str(competitors[resultindex][1]) + " in position " + str(competitors[resultindex][4]) + " with lap time " + str(competitors[resultindex][5])
		old = competitors[resultindex]

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
