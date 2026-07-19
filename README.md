# TVBox 自建央视直播源

域名固定 + 脚本自动维护，TVBox 配置一次，长期使用。

```
NAS（定时跑 fetch.py）或 GitHub Actions
        │  抓上游源 → 过滤央视 → 验活测速 → 每频道留最优 5 条
        ▼  生成 docs/cctv.m3u + docs/tvbox.json 并推送
GitHub 仓库 → GitHub Pages → 绑定你的域名
        ▼
TVBox 配置地址：https://你的域名/tvbox.json   ← 只填这一次
```

## 一、买域名（约 10 元/首年）

1. 在阿里云或腾讯云买 `.top` / `.xyz` 等便宜后缀，例如 `abc-tv.top`。
2. 完成**域名实名认证**（上传身份证，几分钟审核）。
   这只是实名，**不是 ICP 备案**——备案只有用国内大陆服务器建站才需要，
   我们解析到 GitHub Pages（海外），不需要备案。

## 二、建 GitHub 仓库并开启 Pages

1. 新建一个公开仓库（如 `tvbox-live`），把本目录全部文件 push 上去。
2. 仓库 **Settings → Pages**：Source 选 `Deploy from a branch`，
   分支选 `main`、目录选 `/docs`，保存。
3. 同一页面 **Custom domain** 填 `live.xiaoxiaowang.online`（用子域名，省去根域名的麻烦），
   保存并勾选 **Enforce HTTPS**。

## 三、域名解析

在域名 DNS 控制台加一条记录：

| 类型 | 主机记录 | 记录值 |
|------|---------|--------|
| CNAME | `live` | `你的GitHub用户名.github.io` |

等几分钟，`https://live.xiaoxiaowang.online/index.html` 能打开就说明发布成功。

## 四、NAS 定时维护（核心）

NAS 上装好 `git` 和 `python3`（群晖/威联通可用自带套件或 Docker），然后：

```bash
git clone https://github.com/你的用户名/tvbox-live.git
cd tvbox-live
# 先配好 git 推送权限（建议用 Personal Access Token 或 SSH key）
python3 fetch.py --domain live.xiaoxiaowang.online --push   # 手动跑一次验证
```

加定时任务（crontab 或群晖「任务计划」），每 6 小时一次：

```cron
23 */6 * * *  cd /volume1/tvbox-live && python3 fetch.py --domain live.xiaoxiaowang.online --push >> fetch.log 2>&1
```

## 五、TVBox 端配置（只做这一次）

TVBox → 设置 → 配置地址，填入：

```
https://live.xiaoxiaowang.online/tvbox.json
```

以后源失效由脚本自动替换，客户端永远不用动。

## 六、云端保险（可选）

`.github/workflows/update.yml` 已内置：GitHub 每 6 小时也在云端跑一遍同一脚本，
NAS 关机/故障时源照常在更新。启用方法：仓库 **Settings → Secrets and variables →
Actions → Variables** 添加 `TVBOX_DOMAIN = live.xiaoxiaowang.online`。
不需要云端跑就直接删掉 `.github/workflows/update.yml`。

## 常见问题

- **没有 IPv6？** 删掉 `sources.txt` 里 fanmingming 那行（它是 IPv6 源），
  或在验活时它自然会被淘汰，不影响使用。
- **想加更多源？** 往 `sources.txt` 追加 m3u 地址即可，脚本自动过滤央视并验活。
- **画质不理想？** 直播源多为公开网络源，稳定性以验活结果为准；
  想更清晰可以收集更多上游源加进 `sources.txt`。
- **某天 GitHub 访问抽风？** 文件极小且 TVBox 只在打开/刷新配置时拉取，
  偶尔慢不影响已经加载的播放列表。
