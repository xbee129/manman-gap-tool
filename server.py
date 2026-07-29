# -*- coding: utf-8 -*-
"""查漏补缺工具 - 本地服务 (Python 标准库, 无第三方依赖)
布局: 顶部配置(视频/笔记文件夹) + 左树 + 中笔记(搜索/笔记/srt) + 右视频
"""
import os, re, json, base64, urllib.parse, mimetypes, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
MEDIA_EXT = {".mp4", ".mp3", ".m4a", ".wav", ".mov", ".webm", ".mkv"}
SRT_EXT = {".srt"}
NOTE_EXT = {".html", ".htm"}

# ---------- 名称归一化 (视频/笔记/srt 同名匹配) ----------
def norm_key(s):
    s = re.sub(r"\.(mp4|mp3|m4a|wav|mov|webm|mkv|srt|html|htm)$", "", s,
               flags=re.I)
    s = s.lower()
    s = s.replace("\uff08", "(").replace("\uff09", ")")
    s = s.replace("\uff1a", ":").replace("\u2014", "")
    s = re.sub(r"\s+", "", s)
    s = s.replace("\u8bfe", "")  # 删除散落的 课
    s = re.sub(r"^l0-l2", "", s)
    s = re.sub(r"^l[3-6]\d*\.?\s*", "", s)
    s = re.sub(r"^\d+[.\-、\s]*", "", s)
    s = s.replace("(", "").replace(")", "").replace("\uff08", "").replace("\uff09", "")
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", s)
    return s

# ---------- srt 解析 ----------
def t2s(ts):
    ts = ts.strip().replace(",", ".")
    p = [float(x) for x in ts.split(":")]
    while len(p) < 3:
        p.insert(0, 0.0)
    return p[-3] * 3600 + p[-2] * 60 + p[-1]

def parse_srt(text):
    text = text.replace("\r\n", "\n")
    segs = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l for l in block.split("\n") if l.strip() != ""]
        if not lines:
            continue
        tidx = None
        for i, l in enumerate(lines):
            if re.search(r"\d{1,2}:\d{2}[:\.]\d{1,3}(?:[,.]\d{1,3})?\s*-->"
                         r"\s*\d{1,2}:\d{2}[:\.]\d{1,3}(?:[,.]\d{1,3})?", l):
                tidx = i
                break
        if tidx is None:
            continue
        m = re.search(r"(\d{1,2}:\d{2}[:\.]\d{1,3}(?:[,.]\d{1,3})?)\s*-->"
                      r"\s*(\d{1,2}:\d{2}[:\.]\d{1,3}(?:[,.]\d{1,3})?)", lines[tidx])
        if not m:
            continue
        content = "\n".join(lines[tidx + 1:]).strip()
        if not content:
            continue
        segs.append({"start": round(t2s(m.group(1)), 2),
                     "end": round(t2s(m.group(2)), 2),
                     "text": content})
    return segs

# ---------- 目录扫描 ----------
def scan_media(video_dir):
    """递归扫描视频目录, 返回树结构, 叶子带 id 与 path"""
    counter = {"n": 0}
    def walk(d):
        nodes = []
        try:
            entries = sorted(os.listdir(d))
        except Exception:
            return nodes
        for name in entries:
            p = os.path.join(d, name)
            if os.path.isdir(p):
                children = walk(p)
                if children or any(os.path.splitext(e)[1].lower() in MEDIA_EXT
                                   for e in safe_list(p)):
                    nodes.append({"type": "dir", "name": name,
                                  "path": p, "children": children})
            elif os.path.splitext(name)[1].lower() in MEDIA_EXT:
                counter["n"] += 1
                nodes.append({"type": "file", "name": name, "path": p,
                              "id": counter["n"]})
        return nodes
    return walk(video_dir)

def safe_list(d):
    try:
        return os.listdir(d)
    except Exception:
        return []

def collect_support(root_dir):
    """全量遍历课程文件夹, 收集两类支撑文件用于匹配。

    - srt_by_base: {norm(无扩展名): 路径}  , 按"同名"匹配字幕
    - html_list   : [(norm(无扩展名), 路径), ...], 按"文件名包含 MP4 名"匹配笔记
    遍历范围 = 课程文件夹本身(含所有子目录), 不向上扫描。"""
    srt_by_base, html_list = {}, []
    if not root_dir or not os.path.isdir(root_dir):
        return srt_by_base, html_list
    for root, _dirs, files in os.walk(root_dir):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            base = os.path.splitext(f)[0]
            if ext in SRT_EXT:
                k = norm_key(base)
                if k and k not in srt_by_base:
                    srt_by_base[k] = os.path.join(root, f)
            elif ext in NOTE_EXT:
                html_list.append((norm_key(base), os.path.join(root, f)))
    return srt_by_base, html_list

def resolve(tree, srt_by_base, html_list):
    """为树的叶子(MP4 等媒体)建立对应关系:
       - srt : 与叶子同名(归一化后)的 srt 路径
       - note: 文件名(归一化后)包含叶子名的 html 路径, 优先精确同名"""
    for node in tree:
        if node["type"] == "dir":
            resolve(node["children"], srt_by_base, html_list)
        else:
            k = norm_key(node["name"])          # 叶子文件名(已去扩展名)
            node["srt"] = srt_by_base.get(k)
            cand = [hp for hk, hp in html_list if k and k in hk]
            if cand:
                exact = [hp for hk, hp in html_list if hk == k]
                node["note"] = (exact or cand)[0]
            else:
                node["note"] = None

# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8",
              extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _path_param(self):
        q = urllib.parse.urlparse(self.path).query
        return urllib.parse.unquote(urllib.parse.parse_qs(q).get("path", [""])[0])

    def do_GET(self):
        up = urllib.parse.urlparse(self.path)
        route = up.path
        if route in ("/", "/index.html"):
            self._serve_static("index.html")
        elif route == "/video":
            self._serve_video(self._path_param())
        elif route == "/note":
            self._serve_note(self._path_param())
        elif route == "/noteasset":
            self._serve_asset(self._path_param())
        elif route == "/srt":
            self._serve_srt(self._path_param())
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path == "/api/config":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                data = json.loads(raw.decode("utf-8"))
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": str(e)}))
                return
            root_dir = (data.get("root_dir", "") or data.get("video_dir", "")).strip()
            if not os.path.isdir(root_dir):
                self._send(200, json.dumps({"ok": False,
                    "error": "课程文件夹不存在: " + root_dir}))
                return
            # 1) 全量遍历: 目录树(叶子=媒体文件) + 支撑文件(srt/html)
            tree = scan_media(root_dir)
            srt_by_base, html_list = collect_support(root_dir)
            # 2) 建立对应关系: 同名 srt + 文件名包含 MP4 名的 html
            resolve(tree, srt_by_base, html_list)
            # 3) 统计
            leaf = {"total": 0, "with_note": 0, "with_srt": 0}
            def count(nodes):
                for n in nodes:
                    if n["type"] == "dir":
                        count(n["children"])
                    else:
                        leaf["total"] += 1
                        if n.get("note"):
                            leaf["with_note"] += 1
                        if n.get("srt"):
                            leaf["with_srt"] += 1
            count(tree)
            # 4) 持久化数据结构到 JSON 文件(每次加载重新遍历构建)
            payload = {
                "root": root_dir,
                "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "stats": leaf,
                "tree": tree,
            }
            try:
                with open(os.path.join(ROOT, "index.json"), "w",
                          encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
            except Exception:
                pass
            self._send(200, json.dumps({"ok": True, "tree": tree,
                "stats": leaf, "index_file": "index.json"}, ensure_ascii=False))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def _serve_static(self, name):
        p = os.path.join(ROOT, name)
        if not os.path.exists(p):
            self._send(404, "missing")
            return
        with open(p, "rb") as f:
            self._send(200, f.read(),
                       ctype="text/html; charset=utf-8")

    def _serve_video(self, path):
        if not path or not os.path.isfile(path):
            self._send(404, json.dumps({"error": "video missing"}))
            return
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            rng = rng[6:].split(",")[0].strip()
            start, _, end = rng.partition("-")
            start = int(start) if start else 0
            end = int(end) if end else size - 1
            end = min(end, size - 1)
            length = end - start + 1
            with open(path, "rb") as f:
                f.seek(start)
                chunk = f.read(length)
            self.send_response(206)
            self.send_header("Content-Type",
                             mimetypes.guess_type(path)[0] or "video/mp4")
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            self.wfile.write(chunk)
        else:
            with open(path, "rb") as f:
                self._send(200, f.read(),
                           ctype=mimetypes.guess_type(path)[0] or "video/mp4",
                           extra={"Accept-Ranges": "bytes"})

    def _serve_note(self, path):
        if not path or not os.path.isfile(path):
            self._send(404, json.dumps({"error": "note missing"}))
            return
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            self._send(200, f.read(), ctype="text/html; charset=utf-8")

    def _serve_asset(self, path):
        if not path or not os.path.isfile(path):
            self._send(404, json.dumps({"error": "asset missing"}))
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype=ctype)

    def _serve_srt(self, path):
        if not path or not os.path.isfile(path):
            self._send(404, json.dumps({"error": "srt missing"}))
            return
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
            segs = parse_srt(f.read())
        self._send(200, json.dumps({"segments": segs}, ensure_ascii=False))

def main():
    port = int(os.environ.get("PORT", "8770"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print("gap_tool running at http://127.0.0.1:%d" % port)
    srv.serve_forever()

if __name__ == "__main__":
    main()
