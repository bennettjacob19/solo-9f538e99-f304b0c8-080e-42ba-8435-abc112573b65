"""UDP + TCP authoritative DNS server for svc.test., backed by the snapshot
holder.

Each request captures the current Snapshot once and resolves its entire
alias chain against it, so a response never mixes data from two versions.
Publishing never blocks or interrupts serving: the swap is a reference
assignment and old snapshots stay alive for in-flight queries.

Large-answer handling (RFC 1035 §4.2.1 / RFC 6891):

- UDP replies that fit the client-declared capacity are sent whole.
- An oversized UDP reply is trimmed to the largest set of answer RRs that
  still parses within that capacity and the TC bit is set, so a well-behaved
  client retries over TCP. Without an EDNS(0) OPT record the capacity is the
  classic 512-octet limit; with OPT it is the advertised UDP payload size
  (values below 512 are raised to 512). The question section is always kept,
  so the truncated packet remains a valid, parseable response.
- TCP has no 512-octet limit and always returns the complete answer set,
  including the full CNAME chain and every A record.
"""

from dnslib import A, CNAME, EDNS0, QTYPE, RCODE, RR
from dnslib.server import BaseResolver, DNSLogger, DNSServer

from .zone import resolve

CLASSIC_UDP_LIMIT = 512
MIN_EDNS_UDP_SIZE = 512


def _edns_udp_limit(request):
    """UDP payload size advertised by the request.

    Returns (limit, opt_rr_or_None). No OPT record means the classic
    RFC 1035 512-octet ceiling; an OPT record carries the sender's
    EDNS(0) UDP payload size in its CLASS field.
    """
    for rr in request.ar:
        if rr.rtype == QTYPE.OPT:
            limit = max(int(rr.edns_len), MIN_EDNS_UDP_SIZE)
            return limit, rr
    return CLASSIC_UDP_LIMIT, None


def _truncate_to_limit(reply, limit):
    """Trim answer RRs from the end until the wire packet fits `limit`.

    The question section and the EDNS OPT pseudo-record are retained, so the
    returned packet is always a complete, parseable DNS message. Removing RRs
    from the end preserves any leading CNAME chain and keeps as many of the
    final A records as fit.
    """
    reply.header.tc = 1
    while reply.rr and len(reply.pack()) > limit:
        reply.rr.pop()
        reply.set_header_qa()
    return reply


class ZoneResolver(BaseResolver):
    def __init__(self, get_snapshot):
        self._get_snapshot = get_snapshot

    def resolve(self, request, handler):
        reply = request.reply()
        qname = str(request.q.qname).lower()
        try:
            qtype = QTYPE[request.q.qtype]
        except KeyError:
            qtype = "UNKNOWN"

        snapshot = self._get_snapshot()  # captured once: single version per answer
        if snapshot is None:
            reply.header.rcode = RCODE.SERVFAIL  # nothing published yet
            return reply

        rcode, answers = resolve(snapshot, qname, qtype)
        reply.header.rcode = getattr(RCODE, rcode)
        if rcode in ("NOERROR", "NXDOMAIN"):
            reply.header.aa = 1
        for name, rtype, value, ttl in answers:
            rdata = CNAME(value) if rtype == "CNAME" else A(value)
            reply.add_answer(
                RR(rname=name, rtype=getattr(QTYPE, rtype), rclass=1, ttl=ttl, rdata=rdata)
            )

        is_tcp = getattr(handler, "protocol", "udp") == "tcp"
        if is_tcp:
            # TCP carries the full answer set (CNAME chain + every A record).
            return reply

        limit, opt = _edns_udp_limit(request)
        if opt is not None:
            # Echo the OPT pseudo-record (including the DO flag and any EDNS
            # options) so the client sees a matching EDNS(0) response.
            reply.add_ar(
                EDNS0(
                    udp_len=limit,
                    ext_rcode=opt.edns_rcode,
                    version=opt.edns_ver,
                    flags="do" if opt.edns_do else "",
                    opts=list(opt.rdata),
                )
            )
        if len(reply.pack()) > limit:
            _truncate_to_limit(reply, limit)
        return reply


def start_dns_server(get_snapshot, host, port):
    """Start UDP and TCP authoritative listeners on the same host/port.

    Both share one resolver (and therefore one snapshot view): UDP answers
    are truncated with TC when the declared capacity is exceeded, while TCP
    always returns the complete response. Returns the UDP server; both
    threads are daemons and stop with the process.
    """
    resolver = ZoneResolver(get_snapshot)
    # Silence per-query chat on stdout; decode failures still go through log_error.
    logger = DNSLogger(log="-error")
    udp_server = DNSServer(resolver, port=port, address=host, tcp=False, logger=logger)
    tcp_server = DNSServer(resolver, port=port, address=host, tcp=True, logger=logger)
    udp_server.start_thread()
    tcp_server.start_thread()
    return udp_server
