"""
irc/server.py

Copyright by Ferry Boender, 2009

This server has basic support for:

* Connecting
* Channels
* Nicknames
* Public/private messages

It is MISSING support for notably:

* Server linking
* Modes (user and channel)
* Proper error reporting
* Basically everything else

It is mostly useful as a testing tool or perhaps for building something like a
private proxy on. Do NOT use it in any kind of production code or anything that
will ever be connected to by the public.

"""

#
# Very simple hacky ugly IRC server.
#
# Todo:
#   - Encode format for each message and reply with events.codes['needmoreparams']
#   - starting server when already started doesn't work properly. PID file is not changed, no error messsage is displayed.
#   - Delete channel if last user leaves.
#   - [ERROR] <socket.error instance at 0x7f9f203dfb90> (better error msg required)
#   - Empty channels are left behind
#   - No Op assigned when new channel is created.
#   - User can /join multiple times (doesn't add more to channel, does say 'joined')
#   - Error
#     [<socket._socketobject object at 0x26151a0>] [] []
#     ----------------------------------------
#     Exception happened during processing of request from ('127.0.0.1', 47830)
#     Traceback (most recent call last):
#       File "/usr/lib/python2.6/SocketServer.py", line 560, in process_request_thread
#         self.finish_request(request, client_address)
#       File "/usr/lib/python2.6/SocketServer.py", line 322, in finish_request
#         self.RequestHandlerClass(request, client_address, self)
#       File "./hircd.py", line 102, in __init__
#
#       File "/usr/lib/python2.6/SocketServer.py", line 617, in __init__
#         self.handle()
#       File "./hircd.py", line 120, in handle
#         if len(ready_to_read) == 1 and ready_to_read[0] == self.request:
#     error: [Errno 104] Connection reset by peer
#     ----------------------------------------
#   - PING timeouts
#   - Allow all numerical commands.
#   - Users can send commands to channels they are not in (PART)
# Not Todo (Won't be supported)
#   - Server linking.

#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#

from __future__ import print_function, absolute_import

import argparse
import logging
import socket
import select
import re

from . import client
from . import _py2_compat
from . import logging as log_util
from . import events
from . import buffer

SRV_WELCOME = "Welcome to %s v%s, the ugliest IRC server in the world." % (
    __name__, client.VERSION)

log = logging.getLogger(__name__)

class IRCError(Exception):
    """
    Exception thrown by IRC command handlers to notify client of a server/client error.
    """
    def __init__(self, code, value):
        self.code = code
        self.value = value

    def __str__(self):
        return repr(self.value)

class IRCChannel(object):
    """
    Object representing an IRC channel.
    """
    def __init__(self, name, topic='No topic'):
        self.name = name
        self.topic_by = 'Unknown'
        self.topic = topic
        self.clients = set()

class IRCClient(_py2_compat.socketserver.BaseRequestHandler):
    """
    IRC client connect and command handling. Client connection is handled by
    the `handle` method which sets up a two-way communication with the client.
    It then handles commands sent by the client by dispatching them to the
    handle_ methods.
    """
    class Disconnect(BaseException): pass

    def __init__(self, request, client_address, server):
        self.user = None
        self.host = client_address  # Client's hostname / ip.
        self.realname = None        # Client's real name
        self.nick = None            # Client's currently registered nickname
        self.send_queue = []        # Messages to send to client (strings)
        self.channels = {}          # Channels the client is in

        _py2_compat.socketserver.BaseRequestHandler.__init__(self, request,
            client_address, server)

    def handle(self):
        log.info('Client connected: %s', self.client_ident())

        try:
            while self._handle_one():
                pass
        except self.Disconnect:
            self.request.close()

    def _handle_one(self):
        """
        Handle one read/write cycle.
        """
        ready_to_read, ready_to_write, in_error = select.select(
            [self.request], [], [], 0.1)

        # Write any commands to the client
        while self.send_queue:
            msg = self.send_queue.pop(0)
            log.debug('to %s: %s' % (self.client_ident(), msg))
            self.request.send(msg + '\n')

        # See if the client has any commands for us.
        if len(ready_to_read) == 1 and ready_to_read[0] == self.request:
            self._handle_incoming()

    def _handle_incoming(self):
        buf = buffer.LineBuffer()
        data = self.request.recv(1024)

        if not data:
            raise self.Disconnect()
        buf.feed(data)
        for line in buf:
            self._handle_line(line)

    def _handle_line(self, line):
        response = ''
        try:
            log.debug('from %s: %s' % (self.client_ident(),
                line))
            command, sep, params = line.partition(' ')
            handler = getattr(self,
                'handle_%s' % (command.lower()), None)
            if not handler:
                log.info('No handler for command: %s. '
                    'Full line: %s' % (command, line))
                raise IRCError(events.codes['unknowncommand'],
                    '%s :Unknown command' % (command))
            response = handler(params)
        except AttributeError as e:
            raise e
            log.error('%s' % (e))
        except IRCError as e:
            response = ':%s %s %s' % (self.server.servername,
                e.code, e.value)
            log.error('%s' % (response))
        except Exception as e:
            response = ':%s ERROR %s' % (self.server.servername,
                repr(e))
            log.error('%s' % (response))
            raise

        if response:
            log.debug('to %s: %s' % (self.client_ident(),
                response))
            self.request.send(response + '\r\n')

    def handle_nick(self, params):
        """
        Handle the initial setting of the user's nickname and nick changes.
        """
        nick = params

        # Valid nickname?
        if re.search('[^a-zA-Z0-9\-\[\]\'`^{}_]', nick):
            raise IRCError(events.codes['erroneusnickname'], ':%s' % (nick))

        if not self.nick:
            # New connection
            if nick in self.server.clients:
                # Someone else is using the nick
                raise IRCError(events.codes['nicknameinuse'], 'NICK :%s' % (nick))
            else:
                # Nick is available, register, send welcome and MOTD.
                self.nick = nick
                self.server.clients[nick] = self
                response = ':%s %s %s :%s' % (self.server.servername,
                    events.codes['welcome'], self.nick, SRV_WELCOME)
                self.send_queue.append(response)
                response = ':%s 376 %s :End of MOTD command.' % (self.server.servername, self.nick)
                self.send_queue.append(response)
                return
        else:
            if self.server.clients.get(nick, None) == self:
                # Already registered to user
                return
            elif nick in self.server.clients:
                # Someone else is using the nick
                raise IRCError(events.codes['nicknameinuse'], 'NICK :%s' % (nick))
            else:
                # Nick is available. Change the nick.
                message = ':%s NICK :%s' % (self.client_ident(), nick)

                self.server.clients.pop(self.nick)
                self.nick = nick
                self.server.clients[self.nick] = self

                # Send a notification of the nick change to all the clients in
                # the channels the client is in.
                for channel in self.channels.values():
                    for client in channel.clients:
                        if client != self: # do not send to client itself.
                            client.send_queue.append(message)

                # Send a notification of the nick change to the client itself
                return message

    def handle_user(self, params):
        """
        Handle the USER command which identifies the user to the server.
        """
        if params.count(' ') < 3:
            raise IRCError(events.codes['needmoreparams'], 'USER :Not enough parameters')

        user, mode, unused, realname = params.split(' ', 3)
        self.user = user
        self.mode = mode
        self.realname = realname
        return ''

    def handle_ping(self, params):
        """
        Handle client PING requests to keep the connection alive.
        """
        response = ':%s PONG :%s' % (self.server.servername, self.server.servername)
        return (response)

    def handle_join(self, params):
        """
        Handle the JOINing of a user to a channel. Valid channel names start
        with a # and consist of a-z, A-Z, 0-9 and/or '_'.
        """
        channel_names = params.split(' ', 1)[0] # Ignore keys
        for channel_name in channel_names.split(','):
            r_channel_name = channel_name.strip()

            # Valid channel name?
            if not re.match('^#([a-zA-Z0-9_])+$', r_channel_name):
                raise IRCError(events.codes['nosuchchannel'],
                    '%s :No such channel' % (r_channel_name))

            # Add user to the channel (create new channel if not exists)
            channel = self.server.channels.setdefault(r_channel_name, IRCChannel(r_channel_name))
            channel.clients.add(self)

            # Add channel to user's channel list
            self.channels[channel.name] = channel

            # Send the topic
            response_join = ':%s TOPIC %s :%s' % (channel.topic_by, channel.name, channel.topic)
            self.send_queue.append(response_join)

            # Send join message to everybody in the channel, including yourself and
            # send user list of the channel back to the user.
            response_join = ':%s JOIN :%s' % (self.client_ident(), r_channel_name)
            for client in channel.clients:
                #if client != self: # FIXME: According to specs, this should be done because the user is included in the channel listing sent later.
                client.send_queue.append(response_join)

            nicks = [client.nick for client in channel.clients]
            response_userlist = ':%s 353 %s = %s :%s' % (self.server.servername, self.nick, channel.name, ' '.join(nicks))
            self.send_queue.append(response_userlist)

            response = ':%s 366 %s %s :End of /NAMES list' % (self.server.servername, self.nick, channel.name)
            self.send_queue.append(response)

    def handle_privmsg(self, params):
        """
        Handle sending a private message to a user or channel.
        """
        # FIXME: events.codes['needmoreparams']
        target, msg = params.split(' ', 1)

        message = ':%s PRIVMSG %s %s' % (self.client_ident(), target, msg)
        if target.startswith('#') or target.startswith('$'):
            # Message to channel. Check if the channel exists.
            channel = self.server.channels.get(target)
            if channel:
                if not channel.name in self.channels:
                    # The user isn't in the channel.
                    raise IRCError(events.codes['cannotsendtochan'],
                        '%s :Cannot send to channel' % (channel.name))
                for client in channel.clients:
                    # Send message to all client in the channel, except the user himself.
                    # TODO: Abstract this into a seperate method so that not every function has
                    # to check if the user is in the channel.
                    if client != self:
                        client.send_queue.append(message)
            else:
                raise IRCError(events.codes['nosuchnick'], 'PRIVMSG :%s' % (target))
        else:
            # Message to user
            client = self.server.clients.get(target, None)
            if client:
                client.send_queue.append(message)
            else:
                raise IRCError(events.codes['nosuchnick'], 'PRIVMSG :%s' % (target))

    def handle_topic(self, params):
        """
        Handle a topic command.
        """
        if ' ' in params:
            channel_name = params.split(' ', 1)[0]
            topic = params.split(' ', 1)[1].lstrip(':')
        else:
            channel_name = params
            topic = None

        channel = self.server.channels.get(channel_name)
        if not channel:
            raise IRCError(events.codes['nosuchnick'], 'PRIVMSG :%s' % channel_name)
        if not channel.name in self.channels:
            # The user isn't in the channel.
            raise IRCError(events.codes['cannotsendtochan'],
                '%s :Cannot send to channel' % (channel.name))

        if topic:
            channel.topic = topic
            channel.topic_by = self.nick
        message = ':%s TOPIC %s :%s' % (self.client_ident(), channel_name,
            channel.topic)
        return message

    def handle_part(self, params):
        """
        Handle a client parting from channel(s).
        """
        for pchannel in params.split(','):
            if pchannel.strip() in self.server.channels:
                # Send message to all clients in all channels user is in, and
                # remove the user from the channels.
                channel = self.server.channels.get(pchannel.strip())
                response = ':%s PART :%s' % (self.client_ident(), pchannel)
                if channel:
                    for client in channel.clients:
                        client.send_queue.append(response)
                channel.clients.remove(self)
                self.channels.pop(pchannel)
            else:
                response = ':%s 403 %s :%s' % (self.server.servername, pchannel, pchannel)
                self.send_queue.append(response)

    def handle_quit(self, params):
        """
        Handle the client breaking off the connection with a QUIT command.
        """
        response = ':%s QUIT :%s' % (self.client_ident(), params.lstrip(':'))
        # Send quit message to all clients in all channels user is in, and
        # remove the user from the channels.
        for channel in self.channels.values():
            for client in channel.clients:
                client.send_queue.append(response)
            channel.clients.remove(self)

    def handle_dump(self, params):
        """
        Dump internal server information for debugging purposes.
        """
        print("Clients:", self.server.clients)
        for client in self.server.clients.values():
            print(" ", client)
            for channel in client.channels.values():
                print("     ", channel.name)
        print("Channels:", self.server.channels)
        for channel in self.server.channels.values():
            print(" ", channel.name, channel)
            for client in channel.clients:
                print("     ", client.nick, client)

    def client_ident(self):
        """
        Return the client identifier as included in many command replies.
        """
        return '%s!%s@%s' % (self.nick, self.user, self.server.servername)

    def finish(self):
        """
        The client conection is finished. Do some cleanup to ensure that the
        client doesn't linger around in any channel or the client list, in case
        the client didn't properly close the connection with PART and QUIT.
        """
        log.info('Client disconnected: %s' % (self.client_ident()))
        response = ':%s QUIT :EOF from client' % (self.client_ident())
        for channel in self.channels.values():
            if self in channel.clients:
                # Client is gone without properly QUITing or PARTing this
                # channel.
                for client in channel.clients:
                    client.send_queue.append(response)
                channel.clients.remove(self)
        self.server.clients.pop(self.nick)
        log.info('Connection finished: %s' % (self.client_ident()))

    def __repr__(self):
        """
        Return a user-readable description of the client
        """
        return '<%s %s!%s@%s (%s)>' % (
            self.__class__.__name__,
            self.nick,
            self.user,
            self.host[0],
            self.realname,
            )

class IRCServer(_py2_compat.socketserver.ThreadingMixIn,
        _py2_compat.socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    channels = {}
    "Existing channels (IRCChannel instances) by channel name"

    clients = {}
    "Connected clients (IRCClient instances) by nick name"

    def __init__(self, *args, **kwargs):
        self.servername = 'localhost'
        self.channels = {}
        self.clients = {}
        _py2_compat.socketserver.TCPServer.__init__(self, *args, **kwargs)

def get_args():
    parser = argparse.ArgumentParser()

    parser.add_argument("-a", "--address", dest="listen_address",
        default='127.0.0.1', help="IP on which to listen")
    parser.add_argument("-p", "--port", dest="listen_port", default=6667,
        type=int, help="Port on which to listen")
    log_util.add_arguments(parser)

    return parser.parse_args()

def main():
    options = get_args()
    log_util.setup(options)

    log.info("Starting irc.server")

    #
    # Start server
    #
    try:
        bind_address = options.listen_address, options.listen_port
        ircserver = IRCServer(bind_address, IRCClient)
        log.info('Listening on {listen_address}:{listen_port}'.format(
            **vars(options)))
        ircserver.serve_forever()
    except socket.error as e:
        log.error(repr(e))
        raise SystemExit(-2)

if __name__ == "__main__":
    main()
