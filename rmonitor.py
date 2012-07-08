#!/usr/bin/env python
## LineReceiver network client
## Courtesy of Kevin McDermott

#!/usr/bin/env python

from sys import stdout

from twisted.internet.protocol import ReconnectingClientFactory
from twisted.protocols.basic import LineOnlyReceiver
from twisted.internet import reactor


class RaceTimeReceiver(LineOnlyReceiver):
    delimiter = "\n"

    def lineReceived(self, data):
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
        if command == "$COMP":
                print "Competitor information : " + str(data)
        elif command == "$A":
		# $A always comes before $COMP, and $A carries the transponder number
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
        #elif command == "$J":
                # print "Passing information : "  + str(data)



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
    reactor.connectTCP("192.168.10.27", 50000, factory)
    reactor.run()
