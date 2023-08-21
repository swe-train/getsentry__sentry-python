from copy import copy
import json
import time
import threading

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from sentry_sdk._queue import Queue, FullError
from sentry_sdk.utils import logger

from .utils import json_dumps


_TERMINATOR = object()

DEFAULT_RESPONSE = """<!doctype html>
<html>
<head>
        <title>pipe</title>
</head>
<body>
        <pre id="output"></pre>
        <script type="text/javascript">
const Output = document.getElementById("output");
var EvtSource = new EventSource('/stream');
EvtSource.onmessage = function (event) {
        Output.appendChild(document.createTextNode(event.data));
        Output.appendChild(document.createElement("br"));
};
        </script>
</body>
</html>"""

# TODO(dcramer): this is all probably better done as a proper sidecar, which the SDK can then route to (via sockets, vs in-process)

class ProducerConsumer():
    def __init__(self):
        self.lock = threading.Condition()
        self.consumers = set()
        self.waiting = set()
        self.item = None

    def consume(self):
        with self.lock:
            ident = threading.get_ident()
            self.consumers.add(ident)
            while True:
                self.lock.wait_for(lambda: ident in self.waiting)
                self.waiting.remove(ident)
                self.lock.notify()
                try:
                    yield copy(self.item)
                except GeneratorExit:
                    break
            self.consumers.remove(ident)

    def produce(self, item):
        with self.lock:
            self.lock.wait_for(lambda: len(self.waiting) == 0)
            self.item = item
            self.waiting = self.consumers.copy()
            self.lock.notify()

def make_handler(stream: ProducerConsumer):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if self.path == '/stream':
                print("SPOTLIGHT: New Client")
                # self.send_response(200)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Credentials', 'true')
                self.send_header('Content-type', 'text/event-stream')
                self.end_headers()

                for item in stream.consume():
                    if item is _TERMINATOR:
                        break
                    print("SPOTLIGHT: Sending Event")
                    self.wfile.write(f'data: {item.rstrip()}\n\n'.encode())
            else:
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                html = DEFAULT_RESPONSE
                self.wfile.write(html.encode())
        
        def do_POST(self):
            if self.path != '/stream':
                self.send_response(404)
                self.end_headers()
                return
            
            try:
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                stream.produce(data)
            except Exception:
                self.send_response(500)
            else:
                self.send_response(204)

            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Credentials', 'true')
            self.send_header('Content-Type', 'application/json')
            self.end_headers()

            # self.wfile.write("{}")

    return Handler


class SpotlightHttpServer(object):
    def __init__(self, stream: ProducerConsumer, port: int):
        self.port = port
        self.stream = stream
        self.server = None

        self._lock = threading.Lock()
        self._thread = None

    def stop(self):
        with self._lock:
            if self.server is not None:
                self.server.shutdown()
            
            if self._thread is not None:
                while self._thread.is_alive():
                    logger.warning("Waiting for spotlight sidecar to shutdown")
                    time.sleep(100)
                self._thread = None

    def start(self):
        with self._lock:
            if self._thread is not None:
                raise Exception
            if self.server is None:
                self.server = ThreadingHTTPServer(('', self.port), make_handler(self.stream))
            self._thread = threading.Thread(
                target=self.server.serve_forever,
                name="sentry-spotlight.HttpServer"
            )
            self._thread.daemon = True
            self._thread.start()
            logger.info(f"Sidecar starting on :{self.port}")


class SpotlightSidecar(object):
    def __init__(self, port=8000):
        self.port = port
        self.stream = ProducerConsumer()
        
        self._lock = threading.Lock()
        self._http = None
        self._terminated = False

    def ensure(self):
        with self._lock:
            if self._http is None:
                print('SPOTLIGHT: starting sidecar')
                self._http = SpotlightHttpServer(self.stream, self.port)
                self._http.start()
    
    def kill(self):
        print("SPOTLIGHT: got kill request")
        with self._lock:
            if self._http:
                try:
                    self.stream.produce(_TERMINATOR)
                except FullError:
                    logger.debug("background worker queue full, kill failed")

                self._http.stop()
                self._http = None

    def capture_event(self, event):
        if self._terminated:
            return
        # we defer the start here to avoid dealing w/ process managers
        self.ensure()
        with self._lock:
            self.stream.produce(json_dumps(event).decode())

    def __del__(self):
        self.kill()

instance = None

def setup_spotlight(options):
    global instance

    if instance is None:
        instance = SpotlightSidecar(port=8969)
    
    return instance
