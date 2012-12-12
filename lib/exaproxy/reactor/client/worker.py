# encoding: utf-8
"""
server.py

Created by David Farrar  on 2011-11-30.
Copyright (c) 2011 Exa Networks. All rights reserved.
"""

import socket
import errno

from exaproxy.network.errno_list import errno_block


class Client (object):
	eor = ['\r\n\r\n', '\n\n']

	def __init__(self, name, sock, peer, logger):
		self.name = name
		self.sock = sock
		self.peer = peer
		self.reader = self._read(sock)
		self.writer = self._write(sock)

		self.log = logger
		self.blockupload = None

		# start the _read coroutine
		self.reader.next()

	def checkRequest (self, r_buffer):
		# XXX: max buffer size
		for eor in self.eor:
			if eor in r_buffer:
				request, r_buffer = r_buffer.split(eor, 1)
				request = request + eor
				break
		else:
			request, r_buffer = '', r_buffer

		return request, r_buffer

	def checkChunkSize (self, r_buffer, eol):
		# XXX: check for lines that are too long
		chunked = True
		size = 0

		while r_buffer:
			if eol in r_buffer:
				size_s, r_buffer = r_buffer.split(eol, 1)
			else:
				size_s = None

			if size_s is not None:
				if size_s and not size_s.strip('0123456789abcdef'):
					chunk_size = int(size_s, 16)
					size += chunk_size + len(size_s) + (2 * len(eol))

					if chunk_size == 0:
						chunked = False
						break
				else:
					size = None
					break

			elif len(r_buffer) > max_len:
				size = None
				break

			elif r_buffer.rstrip(eol).strip('0123456789abcdef'):
				size = None
				break

		return chunked, size


	def _read (self, sock, read_size=64*1024):
		"""Coroutine managing data read from the client""" 
		eol = '\r\n' # XXX: extrapolate from eor
		r_buffer = ''
		request = ''
		remaining = 0
		r_size, _ = yield ''
		chunked = False

		while True:
			try:
				while True:
					if request:
						# we may have buffered data that was sent along with the request
						# so the coroutine will be asked for data even if there is nothing
						# to read in the socket buffer
						request = ''
					else:
						data = sock.recv(r_size or read_size)
						if data:
							r_buffer += data
						else:          # read failed so we abort
							break

					if remaining > 0:
						length = min(len(r_buffer), remaining)
						related, r_buffer = r_buffer[:length], r_buffer[length:]

						r_size, extra_size = yield '', related, False
						remaining = max(remaining - length + extra_size, 0)

					elif remaining < 0:
						related, r_buffer = r_buffer, ''
						r_size, _ = yield '', related, False

					if remaining != 0:
						continue # we expect that the client will write more data

					if chunked:
						# sum of the sizes of all chunks in our buffer
						chunked, chunk_size = self.checkChunkSize(r_buffer, eol)
						if chunk_size is not None:
							remaining = chunk_size
							continue
						else:
							# we thought we had the start of a new chunk - abort
							break

					# check to see if we have read an entire request
					request, r_buffer = self.checkRequest(r_buffer)

					r_size, remaining = yield request, '', True # yield to manager.readRequest
					if request and remaining == 'chunked':
						chunked = True
						remaining = 0

					elif not request:
						remaining = 0

				# break out of the outer loop as soon as we leave the inner loop
				# through normal execution
				break

			except socket.error, e:
				if e.args[0] in errno_block:
					yield '', '', None
				else:
					break

		yield None
		

	def setPeer (self, peer):
		"""Set the claimed ip address for this client.
		Does not effect the ip address we try sending data to."""
		self.peer = peer

	def readData(self):
		name, peer = self.name, self.peer
		res = self.reader.send((0,0))

		if res is not None:
			request, content, new_request = res
		else:
			request, content, new_request = None, None, None

		#if new_request is False:
		#	raise RuntimeError, 'BAD! We should have a new request'
		#	request, content = None, None

		return name, peer, request, content

	def readRelated(self, remaining):
		name, peer = self.name, self.peer
		res = self.reader.send((0,remaining))

		if res is not None:
			request, content, new_request = res
		else:
			request, content, new_request = None, None, None

		return name, peer, request, content

	def _write(self, sock):
		"""Coroutine managing data sent to the client"""
		
		w_buffer = ''
		filename = yield None

		# check to see if we are returning data directly from a local file
		if filename is not None:
			try:
				# XXX: we must read from the file on demand rather than doing this
				with open(filename) as fd:
					w_buffer = fd.read()

				found = True, False, 0
			except IOError:
				found = None

			data = yield found
			w_buffer = data + w_buffer
		else:
			found = None

		data = yield found
		finished = False

		while True:
			try:
				while True:
					had_buffer = bool(w_buffer)

					if data is not None:
						w_buffer += data
					else:
						# We've finished downloading, even if the client hasn't yet
						finished = True

					if finished:
						if not w_buffer:
							break      # terminate the client connection
						elif data:
							self.log.error('Tried to send data to client after we told it to close. Dropping it.')

					if not had_buffer or data == '':
						sent = sock.send(w_buffer)
						w_buffer = w_buffer[sent:]
					else:
						sent = 0

					buffered = bool(w_buffer) or finished
					data = yield buffered, had_buffer, sent


				# break out of the outer loop as soon as we leave the inner loop
				# through normal execution
				yield None
				break

			except socket.error, e:
				if e.args[0] in errno_block:
					self.log.info('interrupted when trying to sent %d bytes, will retry' % len(data))
					self.log.info('reason: errno %d: %s' % (e.args[0], errno.errorcode.get(e.args[0], '<no errno name>')))
					data = yield bool(w_buffer) or finished, had_buffer, 0
				else:
					self.log.critical('unexpected error writing on socket')
					self.log.critical('reason, errno %d: %s' % (e.args[0], errno.errorcode.get(e.args[0], '<no errno name>')))
					yield None # stop the client connection
					break # and don't come back

		yield None

	def writeData(self, data):
		res = self.writer.send(data)
		return res


	def startData(self, command, data):
		# start the _write coroutine
		self.writer.next()

		if command == 'stream':
			self.writer.send(None)  # no local file
			res = self.writer.send(data)

		elif command == 'close':
			self.writer.send(None)  # no local file
			self.writer.send(data)
			res = self.writer.send(None)  # close the connection once the buffer is empty

		elif command == 'file':
			header, filename = data
			res = self.writer.send(filename)  # use local file
			self.writer.send(header)      # write the response headers before the file
			
			self.writer.send(None)        # close the connection once the buffer is empty
		else:
			res = None

		# buffered, had_buffer
		return res

	def restartData(self, command, data):
		self.writer = self._write(self.sock)
		return self.startData(command, data)

	def shutdown(self):
		try:
			self.sock.shutdown(socket.SHUT_RDWR)
			self.sock.close()
		except socket.error:
			pass

		self.writer.close()
		self.reader.close()
