# AI 卡台

公网：https://ai.mychangliapp.cn/  
后台：https://ai.mychangliapp.cn/admin  
部署日期：2026-10-25（Asia/Shanghai）

这是自有前后端的第一版卡台，采用原生 HTML/CSS/JavaScript、FastAPI、SQLite、单独的履约进程。没有外部字体、追踪脚本或前端框架运行时。视觉参考现有「我的长理」官网线上源码的蓝白配色、1120px 内容宽度与圆角导航，按本次要求移除了说明性小字。

## 当前可用范围

- 商品与周期选择；示例售价为 1 个月 149 元、3 个月 429 元，可在后台修改。
- Session 获取入口；在用户浏览器内解析邮箱与有效期，并核对待充值账号。
- 卡密生成、下载、摘要存储、一次性核销；并发请求通过事务防止重复兑换。
- 订单生成、金额快照、幂等提交、个人会话查单、订单号和查询码查单。
- 管理员登录、商品上下架、订单处理、卡密管理、支付和履约服务设置。
- 支付回调签名、金额与交易编号核验；同一交易不能支付不同订单。
- 履约状态轮询；超时后按订单号查回结果，不盲目重试创建任务。
- 支付和充值通道均留空，因此公网不接受付款或真实充值订单，支付按钮禁用。

Session 的解析只检查格式、邮箱和有效期，不是服务提供方的账号验证。没有抓取别家私有后台、用户令牌或聊天记录。没有完成真实支付、实际会员到账或任何真实账号测试。

## 来源与选择

基于上级 `research/` 中 50 站样本，选取三个参考：

1. UPGPTs：https://upgpts.com/recharge/ —— 卡密、账号核对、确认、查询的短流程。
2. Aileader：https://aileaderme.top/ —— 自有商城与兑换分开；本次复核原站未能稳定打开，采用 10 月 22 日已经保存的证据。
3. Dujiao-Next：https://github.com/dujiao-next/dujiao-next —— 已复核 README、卡密服务与订单状态测试，参考商品、库存事务、支付状态和履约状态分离的结构。

Dujiao-Next 为 Go/Vue 的完整商城，当前代码为本项目独立实现，没有部署或复制其代码；也没有把推广型充值仓库当成可用充值引擎。采用轻量服务是为了本次很窄的卡台范围和现有 Python 服务器环境。

## 本地运行

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8808 --no-access-log
# 另一终端，仅配置通道后处理任务；未配置时仅进行凭证过期清理
.venv/bin/python worker.py
```

首次启动生成 `data/setup-code`，管理员在 `/admin` 输入初始化码、自行设置账号和至少 12 字符的密码。生产环境初始化码另存于所有者本机私有目录，不在仓库、聊天或部署包中。初始化成功后服务器上的码自动失效。

数据目录通过 `CARDDESK_DATA` 配置，同源地址通过 `CARDDESK_ORIGIN` 配置。线上使用 host-only、Secure、HttpOnly、SameSite=Strict Cookie；写接口检查 Origin。

## 支付接口契约

后台可填写 HTTPS 支付链接。创建订单时附加 `order_id` 与 `amount`（元）；不会在链接上附加邮箱、Session 或查询码。仅静态收款码或任意外部链接不能自动确认付款，需要支付服务适配以下回调协议。

`POST /api/payment/callback`

```json
{"order_id":"AI…","amount_cents":14900,"transaction_id":"gateway-unique-id","status":"paid"}
```

请求头：

- `X-Timestamp`：Unix 秒，允许前后 300 秒。
- `X-Signature`：用后台设置的回调密钥计算 `HMAC-SHA256(timestamp + "." + 原始请求体)` 的十六进制摘要。

重复回调不重复履约。金额错误、交易复用或状态冲突拒绝。迟到的已付款通知会转为待更新账号，保留付款事实。真实支付平台如采用不同签名字段、金额单位或回调格式，必须实现其适配器并做沙箱验证，不能仅粘贴链接就宣称接入完成。

## 充值服务接口契约

这是待接实际服务的适配边界，不是任何供应商现成兼容性承诺。上线交易前需选定服务、接收方、实际商品与 API 文档，完成适配和测试，并更新服务信息页。

配置 `服务地址` 与 `接口密钥`。服务器用 `Authorization: Bearer <key>` 请求：

```text
POST {服务地址}/orders
GET  {服务地址}/orders/by-reference/{本站订单号}
```

创建任务请求：

```json
{"reference":"AI…","product":"plus-1","months":1,"account":{"email":"user@example.test","access_token":"<short-lived-token>"}}
```

服务必须保证 `reference` 幂等。返回：

```json
{"reference":"AI…","id":"upstream-task-id","status":"processing"}
```

终态为 `completed` 或 `failed`；完成必须附上非空 `receipt` 凭证编号。只接受匹配本站订单号的结果。未知网络结果保留处理中，随后查回；不会创建第二个充值任务。实际服务不支持这个协议时，应增加适配器，不能伪造返回值。

用户凭证加密暂存最长 30 分钟，提交上游后清除；后台不展示令牌，HTTP 访问日志关闭。下单前没有通道则拒收。支付或队列过期后可以用订单查询码更新同一账号凭证。付款待支付超时关闭，到账通知仍可核对并恢复。交易状态不能通过普通管理员操作直接从未付款改为成功。

## 部署与维护

- 服务账户：`carddesk`，只读源码，数据目录为 `/var/lib/ai-carddesk`。
- 版本目录：`/opt/ai-carddesk/releases/`；当前版本软链接 `/opt/ai-carddesk/current`。
- Web：systemd `ai-carddesk`，仅监听 `127.0.0.1:8808`。
- 任务：systemd `ai-carddesk-worker`，15 秒一轮。
- HTTPS：Nginx 单独虚拟主机，HTTP 自动跳转，证书由 certbot 自动续期。
- DNS：`ai` A 记录指向既有服务器；备注「AI 充值卡台官网及后台」。
- 每日数据库备份：`ai-carddesk-backup.timer`；保留 14 份。备份清除临时凭证、登录会话并 VACUUM，密钥独立存储在同一受保护备份目录。备份与主库仍在同一服务器，尚无异地灾备。
- 回滚：将 `current` 指回保留的旧版本，重启 Web 与 worker；数据库迁移前需单独备份。

后台“处理订单”只记录运营人员核对过的履约或退款结果，不会实际转账。用户查询码只在创建时交付，请妥善保管。卡密完整值只在生成时交付，数据库不保存可恢复原文。

## 验收

15 项自动化测试，覆盖来源检查、管理员隔离、初始化、Session 输入校验、幂等与金额、密文存储、查询权限、并发卡密、回调签名、重复支付、迟到通知、超时不重发、履约凭证和限流。合成测试数据不进入生产库。

浏览器验收覆盖桌面与 390px 手机布局、套餐切换、Session 本地核对、支付按钮禁用、后台登录、商品保存、支付配置和卡密入口。线上检查包括 DNS、HTTPS、健康接口、商品接口、服务进程与既有官网访问。

不将上述测试解释为真实上游完成充值，也不承诺所有国内运营商访问速度。
