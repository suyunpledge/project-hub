"""Generate hub/static/icon.png and icon.ico (stdlib only, 4x supersampled)."""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "hub" / "static"


def render(size: int) -> bytes:
    ss = 4
    n = size * ss
    acc = [[0.0] * 4 for _ in range(size * size)]
    c1, c2 = (0x6D, 0x8C, 0xFF), (0xB0, 0x7C, 0xFF)
    bg = (0x16, 0x19, 0x22)
    r = 0.22 * n  # corner radius
    for y in range(n):
        for x in range(n):
            # rounded-square background
            dx = max(r - x, 0, x - (n - 1 - r))
            dy = max(r - y, 0, y - (n - 1 - r))
            if dx * dx + dy * dy > r * r:
                continue
            u, v = (x + .5) / n - .5, (y + .5) / n - .5
            d = abs(u) + abs(v)
            # stacked diamonds: big gradient one, dark inner cut, small accent core
            if d < 0.36 and not (0.20 < d < 0.27):
                t = (u + v + 0.7) / 1.4
                col = tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))
            else:
                col = bg
            px = acc[(y // ss) * size + x // ss]
            px[0] += col[0]; px[1] += col[1]; px[2] += col[2]; px[3] += 255
    k = ss * ss
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            px = acc[y * size + x]
            a = px[3] / k
            if a:
                raw += bytes((int(px[0] / (px[3] / 255)), int(px[1] / (px[3] / 255)),
                              int(px[2] / (px[3] / 255)), int(a)))
            else:
                raw += b"\0\0\0\0"
    return png(size, bytes(raw))


def png(size: int, raw: bytes) -> bytes:
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def ico(images: dict[int, bytes]) -> bytes:
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, data = b"", b""
    for size, blob in images.items():
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        data += blob
    return head + entries + data


if __name__ == "__main__":
    imgs = {s: render(s) for s in (256, 64, 48, 32, 16)}
    (OUT / "icon.png").write_bytes(imgs[256])
    (OUT / "icon.ico").write_bytes(ico(imgs))
    print("wrote", OUT / "icon.png", OUT / "icon.ico")
