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
        print "Line received %s" % data


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
    reactor.connectTCP("192.168.11.254", 5555, factory)
    reactor.run()
