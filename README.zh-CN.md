# jjwxc_dl

[English](README.md) · [Русский](README.ru.md) · **简体中文**

基于 Playwright 的断点续传下载工具，把你的账号**有权限阅读**的晋江文学城章节
保存为 EPUB。

改写自 [tracywong117/jjwxc-downloader](https://github.com/tracywong117/jjwxc-downloader)
（MIT 许可）。具体改动及原因见[致谢](#致谢)。

## 它做什么，不做什么

它读取**你的账号本来就能打开**的章节，生成带封面和目录的 EPUB。

- 免费章节完全不需要账号。
- VIP 章节需要已登录**并且已购买**。登录方式是打开一个真实浏览器由你亲手完成，
  工具不接触、不保存你的密码。
- 你没有购买的章节会被标记为 `paywalled`，在结束时汇总报告，并且不写入 EPUB，
  也不会用占位文本填充。

这不是绕过付费墙的工具，也不触碰任何技术保护措施。想看书就买：`fetch` 命令会
提前打印出该作品 VIP 部分的大致花费。

> **请把生成的文件留在自己手里。** 晋江会在 VIP 章节正文中嵌入与购买账号绑定的
> 水印。你分享出去的文件可以指向你本人。

## 环境要求

Python 3.9 以上，且 Playwright 能在该系统安装 Chromium。已在
Python 3.14 / Windows 11 与 Python 3.10 上测试。

```bash
pip install -r requirements.txt
```

```bash
python -m playwright install chromium
```

## 使用方法

`novelid` 就在作品页面的网址里：
`jjwxc.net/onebook.php?novelid=`**`1234567`**。

### 1. 下载

```bash
python jjwxc_dl.py fetch 1234567
```

```
某本书 - 某作者
305 chapters: 21 free (65,284 chars), 284 VIP (1,071,596 chars)
VIP portion costs roughly 5358 coins (~53.6 CNY) at the standard 5 fen/1k rate
305 chapters to fetch

[1/305] 第一章 (free) ... ok, 3021 chars
[22/305] 第二十二章 (VIP) ... paywalled
```

每一章都单独写入一个 JSON 检查点，所以随时 Ctrl+C 都是安全的：再次运行同一条
命令会跳过已成功的章节，只重试失败的部分。

### 2. 登录（一次即可，用于 VIP 章节）

```bash
python jjwxc_dl.py login
```

浏览器会打开晋江首页。你自己登录，然后回到终端按 Enter。会话保存在脚本旁边的
`.jjwxc-profile/` 目录中，后续 `fetch` 会直接复用。不要让两个实例同时使用同一
个配置目录——Chromium 会将其锁定给单个进程。

### 3. 生成 EPUB

```bash
python jjwxc_dl.py build 1234567
```

输出 `downloads/<novelid>/<书名>.epub`，并列出其中缺少的章节。

### 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--delay` | `2.0` | 章节之间的基础间隔秒数；实际等待为 `delay` 到 `2×delay`。 |
| `--timeout` | `20.0` | 单页超时秒数。网络慢时调高。 |
| `--retries` | `3` | 每章重试次数，间隔递增。 |
| `--headless` | 关闭 | 隐藏浏览器窗口。首次运行建议保持关闭。 |
| `--out` | `downloads` | 检查点与 EPUB 的输出目录。 |
| `--profile` | `.jjwxc-profile` | 保存登录会话的浏览器配置目录。 |

**请不要调低 `--delay`。** 三百多个请求全速连发是最快触发限流的方式，而且对一个
你正在付费的网站来说并不礼貌。

## 目录结构

```
downloads/<novelid>/
├─ meta.json              书名、作者
├─ cover.jpg
├─ <书名>.epub
└─ chapters/
   └─ 0001.json           index, title, url, vip, words, text, status
```

`status` 的取值为 `ok`、`paywalled`、`error`。

## 疑难排查

**已购买的章节仍显示 `paywalled`** —— 会话没有加载成功。重新运行 `login`，确认
打开的窗口里确实处于登录状态，并检查 `fetch` 与 `login` 使用的是同一个
`--profile`。

**出现大量 `error`** —— 先调高 `--timeout`，再调高 `--delay`。重新运行只会重试
失败的章节。

**所有章节都报错** —— 很可能是页面结构变了，`div.noveltext` 选择器不再匹配。请
去掉 `--headless` 运行，看一下页面，并附上你看到的内容提交 issue。

**打印章节名时出现 `UnicodeEncodeError`** —— 理论上不会发生，工具会强制把自己的
输出流设为 UTF-8。若仍出现，请反馈你的操作系统和终端。

## 致谢

改写自 [tracywong117/jjwxc-downloader](https://github.com/tracywong117/jjwxc-downloader)，
其确立的思路在此保留：解析目录页得到章节列表，正文取 `div.noveltext` 子元素中
文本最多的那一个。

主要改动：

- **用 Playwright 取代 Selenium。** 持久化浏览器上下文意味着只需登录一次，VIP
  章节才第一次变得可达；无需再匹配 `chromedriver.exe` 的版本；定位器自带等待。
- **正确解析 VIP 行。** 付费章节没有 `href`，目标地址放在 `rel=` 中并指向
  `my.jjwxc.net`，点击由 JS 处理。只按 `href` 匹配会静默跳过书中所有付费章节。
- **遍历表格行，而不是 `zip()`** 两个各自独立的 XPath 结果列表——后者一旦两个
  列表长度不一致，就会把章节名和网址配错。
- **可续传。** 每章单独写 JSON 检查点，而不是往一个不断增长的 `.txt` 里追加；
  后者在第二次运行时会把整本书重复写一遍。
- **记录未购买的章节**，而不是把占位文本写进书里。
- **生成合法的 EPUB。** 正文在插入 XHTML 前先转义，孤立的 `<` 或 `&` 不再导致
  文件损坏，并补上语言元数据。
- **读取每章字数**（来自目录页的 `wordCount`），花费估算即由此得出。
- 强制 stdout/stderr 使用 UTF-8，避免非 UTF-8 终端中断长时间下载。

## 许可

MIT，见 [LICENSE](LICENSE)；原作者的版权声明已保留。
