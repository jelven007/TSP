"""Exercise real native sockets against a deterministic local TDX server."""

import json
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

LISTING = b"gpcw20260630.zip,0123456789abcdef0123456789abcdef,123\r\n"


@pytest.fixture(autouse=True)
def _isolate_application_native_pool():
    from app.plugins.rustdx.client import close_native_pool

    close_native_pool()
    yield
    close_native_pool()


def _exact(connection, count):
    result = b""
    while len(result) < count:
        block = connection.recv(count - len(result))
        if not block:
            raise EOFError
        result += block
    return result


def _quote(market, code):
    # All variable integers are zero. amount uses the upstream TDX zero sentinel.
    return bytes([market]) + code + b"\0" * (2 + 9 + 4 + 4 + 20 + 2 + 4 + 4)


class _Server:
    def __init__(
        self,
        *,
        incomplete=False,
        financial=False,
        invalid_listing=False,
        valid_bars=False,
        report_delay=0,
        catalog_delay=0,
        bar_delay=0,
        xdxr_delay=0,
    ):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(100)
        self.listener.settimeout(0.2)
        self.address = f"127.0.0.1:{self.listener.getsockname()[1]}"
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.total = 0
        self.live = 0
        self.peak = 0
        self.batches = []
        self.incomplete = incomplete
        self.financial = financial
        self.invalid_listing = invalid_listing
        self.valid_bars = valid_bars
        self.report_delay = report_delay
        self.catalog_delay = catalog_delay
        self.bar_delay = bar_delay
        self.xdxr_delay = xdxr_delay
        self.report_started = threading.Event()
        self.opcodes = {}
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with self.lock:
                self.total += 1
                self.live += 1
                self.peak = max(self.peak, self.live)
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection):
        try:
            with connection:
                connection.settimeout(3)
                while not self.closed.is_set():
                    header = _exact(connection, 10)
                    request = _exact(connection, struct.unpack_from("<H", header, 6)[0])
                    with self.lock:
                        opcode = request[:2].hex()
                        self.opcodes[opcode] = self.opcodes.get(opcode, 0) + 1
                    if self.financial and request[:2] == b"\x4e\x04":
                        break
                    response = b"\0\0"
                    if request[:2] == b"\x3e\x05":
                        count = struct.unpack_from("<H", request, 10)[0]
                        items = [
                            (request[i], request[i + 1 : i + 7])
                            for i in range(12, 12 + count * 7, 7)
                        ]
                        with self.lock:
                            self.batches.append(items)
                        time.sleep(0.02)
                        returned = [] if self.incomplete else list(reversed(items))
                        response = (
                            b"\0\0"
                            + struct.pack("<H", len(returned))
                            + b"".join(_quote(market, code) for market, code in returned)
                        )
                    elif request[:2] == b"\xb9\x06":
                        self.report_started.set()
                        time.sleep(self.report_delay)
                        offset = struct.unpack_from("<I", request, 2)[0]
                        data = (b"invalid finance index\r\n" if self.invalid_listing else LISTING) if offset == 0 else b""
                        response = struct.pack("<I", len(data)) + data
                    elif request[:2] == b"\x4e\x04":
                        response = struct.pack("<H", 2_100)
                    elif request[:2] == b"\x50\x04":
                        time.sleep(self.catalog_delay)
                        start = struct.unpack_from("<H", request, 4)[0]
                        codes = [f"{i:06d}".encode() for i in range(start, min(start + 1_000, 2_100))]
                        response = struct.pack("<H", len(codes)) + b"".join(
                            code + struct.pack("<H", 100) + b"test\0\0\0\0"
                            + b"\0" * 4 + b"\x02" + b"\0" * 8
                            for code in codes
                        )
                    elif request[:2] == b"\x4d\x04":
                        time.sleep(self.catalog_delay)
                        start, count = struct.unpack_from("<II", request, 4)
                        codes = [f"{i:06d}".encode() for i in range(start, min(start + count, 2_100))]
                        response = struct.pack("<H", len(codes)) + b"".join(
                            code + struct.pack("<H", 100) + b"test" + b"\0" * 12
                            + b"\0" * 4 + b"\x02" + b"\0" * 8
                            for code in codes
                        )
                    elif request[:2] == b"\x04\x00":
                        response = b"\0" * 10
                    elif request[:2] == b"\x2d\x05":
                        time.sleep(self.bar_delay)
                        response = b"\0\0" if self.valid_bars else b"\x01"
                    elif request[:2] == b"\x0f\x00":
                        time.sleep(self.xdxr_delay)
                        response = b"\0" * 11
                    reply = bytearray(16)
                    reply[:4] = b"\xb1\xcb\x74\0"
                    reply[5:9] = header[1:5]
                    reply[10:12] = request[:2]
                    reply[12:16] = struct.pack("<HH", len(response), len(response))
                    # Split the header to prove native read_exact tolerates TCP fragmentation.
                    connection.sendall(reply[:5])
                    connection.sendall(reply[5:] + response)
        except (EOFError, OSError):
            pass
        finally:
            with self.lock:
                self.live -= 1

    def close(self):
        self.closed.set()
        self.listener.close()
        self.thread.join(timeout=1)


def test_native_pool_reuses_8_business_sockets_and_preserves_response_identity():
    module = pytest.importorskip("tsp_rustdx_native")
    server = _Server()
    client = module.RustdxClient(100, server.address, 2)
    securities = [(0, f"{index:06d}") for index in range(2_100)]
    securities += [(1, "000001"), (0, "000001")]  # mixed markets + duplicate
    try:
        first = json.loads(client.quotes_json(securities))
        opened = server.total
        assert opened == 11  # three one-shot probes + eight persistent business sockets
        assert [(row["market"], row["code"]) for row in first] == securities
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(client.quotes_json, securities) for _ in range(2)]
            futures.append(pool.submit(client.report_file, "tdxfin/gpcw.txt", 512_000))
            assert len(json.loads(futures[0].result())) == len(securities)
            assert len(json.loads(futures[1].result())) == len(securities)
            assert bytes(futures[2].result()) == LISTING
        assert server.total == opened
        assert server.peak <= 9
        assert all(
            len(batch) <= 60 and len({market for market, _ in batch}) == 1
            for batch in server.batches
        )
        stats = json.loads(client.stats_json())
        assert stats["total_connections"] == 8
        assert stats["network"]["peak"] <= 8
        assert stats["probes"]["peak"] <= 1
        client.close()
        assert json.loads(client.stats_json())["closed"] is True
        with pytest.raises(RuntimeError, match="closed"):
            client.quotes_json([(0, "000001")])
        assert server.total == opened
    finally:
        client.close()
        server.close()


def test_native_empty_batch_fails_after_bounded_retries():
    module = pytest.importorskip("tsp_rustdx_native")
    server = _Server(incomplete=True)
    client = module.RustdxClient(1, server.address, 2)
    try:
        with pytest.raises(RuntimeError, match="incomplete"):
            client.quotes_json([(0, "000001")])
        assert len(server.batches) == 3
        assert json.loads(client.stats_json())["total_connections"] == 0
    finally:
        client.close()
        server.close()


def test_quote_metadata_warmup_uses_batch_budget_not_one_socket_timeout():
    module = pytest.importorskip("tsp_rustdx_native")
    server = _Server(catalog_delay=1.2)
    client = module.RustdxClient(1, server.address, 2)
    try:
        started = time.monotonic()
        rows = json.loads(client.quotes_json([(0, "000001")]))
        assert rows[0]["code"] == "000001"
        assert time.monotonic() - started > 2
    finally:
        client.close()
        server.close()


def test_security_directory_uses_pagination_budget_not_one_socket_timeout():
    module = pytest.importorskip("tsp_rustdx_native")
    server = _Server(catalog_delay=1.2)
    client = module.RustdxClient(1, server.address, 2)
    try:
        started = time.monotonic()
        rows = json.loads(client.stocks_json(0))
        assert len(rows) == 2_100
        assert time.monotonic() - started > 2
    finally:
        client.close()
        server.close()


def test_financial_server_switch_uses_existing_slots():
    module = pytest.importorskip("tsp_rustdx_native")
    quotes = _Server()
    reports = _Server(financial=True)
    client = module.RustdxClient(1, quotes.address, 2)
    try:
        client.quotes_json([(0, "000001")])
        assert (
            bytes(client.report_file("tdxfin/gpcw.txt", 512_000, reports.address))
            == LISTING
        )
        assert json.loads(client.stats_json())["total_connections"] == 1
        assert reports.opcodes.get("4e04", 0) == 0
        assert reports.opcodes["0400"] == 3
        finance_pool = json.loads(client.stats_json())["financial_pools"][0]
        assert all(
            row["mode"] == "FinancialHeartbeat" and row["security_count"] is None
            for row in finance_pool["results"][0]["rounds"]
        )
        client.quotes_json([(0, "000001")])
        assert json.loads(client.stats_json())["total_connections"] == 1
    finally:
        client.close()
        quotes.close()
        reports.close()


def test_native_fails_over_to_configured_server_without_extra_sockets():
    module = pytest.importorskip("tsp_rustdx_native")
    broken = _Server(incomplete=True)
    healthy = _Server()
    client = module.RustdxClient(1, broken.address, 2, fallback_servers=[healthy.address])
    try:
        rows = json.loads(client.quotes_json([(0, "000001")]))
        assert rows[0]["code"] == "000001"
        stats = json.loads(client.stats_json())
        assert stats["active_server"] == healthy.address
        assert stats["retries"] == 1
        assert stats["total_connections"] == 1
        client.quotes_json([(0, "000001")])
        assert len(broken.batches) == 1
        assert healthy.total == 4  # three probes + one reused business socket
    finally:
        client.close()
        broken.close()
        healthy.close()


def test_background_budget_and_counted_minute_requests():
    module = pytest.importorskip("tsp_rustdx_native")
    server = _Server()
    client = module.RustdxClient(4, server.address, 2, background_connections=1)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(
                lambda _: client.report_file("tdxfin/gpcw.txt", 512_000), range(4),
            ))
        assert all(bytes(value) == LISTING for value in values)
        assert json.loads(client.stats_json())["scheduler"]["background_peak"] == 1
        result = json.loads(client.bars_json(0, "000001", 8, with_request_count=True))
        assert result["requests"] == 3  # truncated transport frames retry at most three times
        assert result["error"]
        stats = json.loads(client.stats_json())
        assert stats["minute_requests"] == 3
        assert stats["scheduler"]["background_limit"] == 1
        assert stats["scheduler"]["waiting_requests"] == 0
    finally:
        client.close()
        server.close()


def test_native_bar_pages_pin_connection_and_reject_replacement():
    module = pytest.importorskip("tsp_rustdx_native")
    server = _Server(valid_bars=True)
    reports = _Server(financial=True)
    client = module.RustdxClient(1, server.address, 2)
    try:
        first = json.loads(client.bars_json(0, "000001", 8, with_source=True))
        assert first["error"] is None
        source = first["source"]
        assert source["endpoint"] == server.address
        second = json.loads(client.bars_json(
            0, "000001", 8, start=800, with_source=True,
            expected_source=json.dumps(source),
        ))
        assert second["source"] == source
        client.report_file("tdxfin/gpcw.txt", 512_000, reports.address)
        replaced = json.loads(client.bars_json(
            0, "000001", 8, start=1600, with_source=True,
            expected_source=json.dumps(source),
        ))
        assert "[SourceChanged]" in replaced["error"]
        assert replaced["requests"] == 0
    finally:
        client.close()
        server.close()
        reports.close()


def test_bar_queue_wait_does_not_consume_single_transport_timeout():
    module = pytest.importorskip("tsp_rustdx_native")
    server = _Server(valid_bars=True, report_delay=1.1, bar_delay=1.1)
    client = module.RustdxClient(1, server.address, 2)
    try:
        initial = json.loads(client.bars_json(0, "000001", 8, with_source=True))
        assert initial["error"] is None
        with ThreadPoolExecutor(max_workers=2) as pool:
            report = pool.submit(client.report_file, "tdxfin/gpcw.txt", 512_000)
            assert server.report_started.wait(timeout=5)
            page = pool.submit(
                client.bars_json, 0, "000001", 8, 800, 800, False, True, True,
                json.dumps(initial["source"]),
            )
            result = json.loads(page.result(timeout=15))
            assert bytes(report.result(timeout=15)) == LISTING
        assert result["error"] is None
        assert result["requests"] == 1
        assert result["source"] == initial["source"]
        assert json.loads(client.stats_json())["scheduler"]["waiting_requests"] == 0
        assert server.peak <= 2  # one business connection and one probe
    finally:
        client.close()
        server.close()


def test_real_bar_transport_timeout_retries_boundedly_and_pool_recovers():
    module = pytest.importorskip("tsp_rustdx_native")
    server = _Server(valid_bars=True, bar_delay=2.2)
    client = module.RustdxClient(1, server.address, 2)
    try:
        started = time.monotonic()
        failed = json.loads(client.bars_json(0, "000001", 8, with_source=True))
        assert "[TimedOut]" in failed["error"]
        assert failed["requests"] == 3
        assert time.monotonic() - started < 12
        stats = json.loads(client.stats_json())
        assert stats["total_connections"] == 0
        assert stats["scheduler"]["waiting_requests"] == 0
        server.bar_delay = 0
        recovered = json.loads(client.bars_json(0, "000001", 8, with_source=True))
        assert recovered["error"] is None and recovered["requests"] == 1
        assert json.loads(client.stats_json())["total_connections"] == 1
        assert server.peak <= 2
    finally:
        client.close()
        server.close()


def test_single_business_request_also_keeps_queue_outside_transport_timeout():
    module = pytest.importorskip("tsp_rustdx_native")
    server = _Server(report_delay=1.1, xdxr_delay=1.1)
    client = module.RustdxClient(1, server.address, 2)
    try:
        assert json.loads(client.xdxr_json(0, "000001")) == []
        with ThreadPoolExecutor(max_workers=2) as pool:
            report = pool.submit(client.report_file, "tdxfin/gpcw.txt", 512_000)
            assert server.report_started.wait(timeout=5)
            request = pool.submit(client.xdxr_json, 0, "000001")
            assert json.loads(request.result(timeout=15)) == []
            assert bytes(report.result(timeout=15)) == LISTING
        assert json.loads(client.stats_json())["scheduler"]["waiting_requests"] == 0
        assert server.peak <= 2
    finally:
        client.close()
        server.close()


def test_financial_index_invalid_fails_without_retrying():
    module = pytest.importorskip("tsp_rustdx_native")
    server = _Server(financial=True, invalid_listing=True)
    client = module.RustdxClient(1, server.address, 2)
    try:
        with pytest.raises(RuntimeError, match="InvalidData"):
            client.report_file("tdxfin/gpcw.txt", 512_000, server.address)
        assert server.opcodes["b906"] == 2  # data + EOF for the single download
        assert json.loads(client.stats_json())["retries"] == 0
    finally:
        client.close()
        server.close()


def test_global_budget_change_requires_restart(monkeypatch, tmp_path):
    module = pytest.importorskip("tsp_rustdx_native")
    module.RustdxClient(1).close()
    monkeypatch.setenv("RUSTDX_NETWORK_SHARED_DIR", str(tmp_path / "permits"))
    with pytest.raises(RuntimeError, match="restart"):
        module.RustdxClient(1)


def test_report_download_has_one_file_budget_and_per_chunk_socket_timeout():
    module = pytest.importorskip("tsp_rustdx_native")
    server = _Server(financial=True, report_delay=0.55)
    client = module.RustdxClient(1, server.address, 2)
    try:
        started = time.monotonic()
        data = client.report_file("tdxfin/gpcw20260630.zip", 512_000, server.address)
        assert bytes(data) == LISTING
        assert time.monotonic() - started > 2
        assert json.loads(client.stats_json())["retries"] == 0
    finally:
        client.close()
        server.close()
