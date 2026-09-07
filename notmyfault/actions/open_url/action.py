"""打开链接动作：用 webbrowser 调用系统默认浏览器
未写协议头的 URL 自动补上 https://
"""

import re
import webbrowser

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def run(action_info, params):
    raw = params.get("urls", "")
    urls = [line.strip() for line in str(raw).splitlines() if line.strip()]
    if not urls:
        raise ValueError("没有可打开的链接")

    new_window = bool(params.get("new_window", False))
    normalized = []
    for url in urls:
        if not _SCHEME_RE.match(url):
            # 未写协议头时按域名补上 https://
            url = "https://" + url
        elif not url.lower().startswith(("http://", "https://")):
            # 自定义协议头会拉起任意注册程序，只放行 http/https
            raise ValueError(f"只支持 http/https 链接: {url}")
        normalized.append(url)
    opened = []
    for url in normalized:
        print("[Action:open_url] 打开 HTTP(S) 链接")
        if webbrowser.open(url, new=1 if new_window else 0):
            opened.append(url)

    if not opened:
        raise RuntimeError("浏览器未能打开任何链接")
    print(f"[Action:open_url] 已打开 {len(opened)} 个链接")
    return {"opened": len(opened)}
