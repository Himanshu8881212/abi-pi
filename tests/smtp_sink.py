"""A local SMTP sink for tests: accepts every message on 127.0.0.1:2525 (or the port given) and appends it to the file given."""
import socketserver
import sys

OUT = sys.argv[1]


class H(socketserver.StreamRequestHandler):
    def handle(self):
        w = lambda s: self.wfile.write((s + "\r\n").encode())  # noqa: E731
        w("220 sink")
        data = None
        for raw in self.rfile:
            line = raw.decode(errors="replace").rstrip("\r\n")
            if data is not None:
                if line == ".":
                    open(OUT, "a").write("\n".join(data) + "\n=====\n")
                    data = None
                    w("250 ok")
                else:
                    data.append(line[1:] if line.startswith("..") else line)
                continue
            cmd = line[:4].upper()
            if cmd == "EHLO":
                w("250-sink")
                w("250 8BITMIME")
            elif cmd == "DATA":
                data = []
                w("354 go")
            elif cmd == "QUIT":
                w("221 bye")
                return
            else:
                w("250 ok")


socketserver.ThreadingTCPServer.allow_reuse_address = True
socketserver.ThreadingTCPServer(("127.0.0.1", int(sys.argv[2]) if len(sys.argv) > 2 else 2525), H).serve_forever()
