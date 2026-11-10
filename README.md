# meterhub · 码表数据互传

把骑行记录在**任意两个平台之间**搬来搬去：Garmin、Wahoo、Strava、行者、iGPSPORT、
以及任何能导出 `.fit/.gpx/.tcx` 的码表（Bryton、迈金、黑鸟、Cateye、COOSPO…）。

## 快速开始

```bash
python -m meterhub                    # 打开本地界面 http://127.0.0.1:8788
python -m meterhub state              # 看各平台配置状态
python -m meterhub transfer xingzhe igpsport --limit 5
python -m meterhub import ./export.zip
python -m meterhub lib
```

安装依赖（联网部分需要 `curl_cffi` 的 TLS 指纹，防 403）：

```bash
pip install -r requirements.txt
```

离线部分（FIT/GPX/TCX 解析、转换、入库、导出）**只用标准库**，不装任何东西也能跑。

## 架构

```
  行者 ─┐                                   ┌─ iGPSPORT
Garmin ─┼─→  适配器(pull)  →  本地 FIT 库  →  适配器(push)  ─┼─ Wahoo
Bryton ─┘        ↑                              ↓            └─ Strava
             Garmin/Strava 导出 zip、码表 SD 卡     导出文件给只能手动的平台
```

- **N 个平台只需 N 个适配器**，不是 N×N 条链路。新增平台只写一个文件。
- 中转格式统一 **FIT**（唯一能同时保心率/踏频/功率/海拔/轨迹的通用格式）。
- 先入本地库再推送：中途失败可重跑，平台接口抽风也不会丢数据。
- 去重三路：目标平台现有记录 + 本地库「已同步到该目标」标记 + 本次运行指纹集合。

## 平台支持情况

| 平台 | 拉取 | 推送 | 需要什么 | 实现状态 |
|---|---|---|---|---|
| 行者 | ✅ v1 API | —（无上传接口） | **手机行者 App 扫码**（短信通道已关） | 已用真实数据验证 |
| iGPSPORT | ✅ 列表 + 原始 FIT 下载 | ✅ OSS 直传 FIT | 手机 + 短信验证码 | 读+写都已实测 |
| 迈金 顽鹿 | ⚠️ 列表可读（官网 `capability_list`，固定 20 条/页按 `total` 翻完）；FIT 下载签名直链当前 404，暂不能导出 | ✅ `upload/fit`（字段 **`jilu0`**） | 顽鹿账号+密码 | 写已实测；去重按 `date + totalDistance` 比对，云端列表读全时**只认云端实况**，本地「已同步」标记不再单独决定跳过；读方向的 FIT 下载待通 |
| Garmin 佳明 | ✅ 活动列表 + 原始 FIT 下载 | ✅ `upload-service` | **手机号**+密码（中国站账号就是手机号；可带 MFA） | 读+写都已实测 |
| Strava | ✅ 官方 API | ✅ 官方 API | 自建 API 应用：Client ID/Secret + refresh_token | 已实现，需你的应用凭据才能实测 |
| Wahoo | —（无文件接口） | ✅ `workout_file_uploads` | Wahoo 开发者应用（需审核） | 已实现，待审核通过实测 |
| 本地文件 | ✅ 文件夹/zip 递归 | ✅ 写 FIT | 无 | 已验证 |

### 四家码表互传：9 个方向

方向不是写死的，界面按「谁能读 × 谁能写」自动生成。行者/iGPSPORT/佳明/迈金
都能读，iGPSPORT/佳明/迈金 都能写，所以是 **4 × 3 − 3（排除自己）= 9 个**：

```
行者   → iGPSPORT   行者   → 佳明   行者   → 迈金
iGPSPORT → 佳明     iGPSPORT → 迈金
佳明   → iGPSPORT   佳明   → 迈金
迈金   → iGPSPORT   迈金   → 佳明
```

**为什么不是 12 个**：行者没有上传接口（网页端也不给），所以没有任何平台能推进行者，
缺的 3 个方向是「→行者」。这是平台能力限制，不是漏做。

完整生态与开放程度见 [`码表互通矩阵.md`](码表互通矩阵.md)。

## 界面

四个动作，一个页面：

1. **来源** — 只有声明了 `pull` 的平台才出现在下拉里
2. **目标** — 只有声明了 `push` 的才出现
3. **开始传输 / 试运行** — 试运行会读完、算好、只打印计划，不写任何数据
4. **手动导入** — 把 Garmin/Strava 导出包、SD 卡目录丢进去入本地库

凭据都在页面底部折叠区，只写到本地 `data/config.json`，不回显。

## 目录

```
meterhub/
  meterhub/
    model.py        Ride / RidePoint / 指纹
    fitlib.py       FIT 写入（FIT 编码器）
    fitread.py      FIT 读取（含压缩时间戳、数组字段、无效值）
    gpxio.py        GPX / TCX 读写
    bundle.py       文件夹/zip/单文件批量导入、批量导出
    store.py        本地库（FIT 落地 + index.json + 同步标记）
    pipeline.py     传输编排（去重、错误隔离、报告）
    adapters/       各平台适配器（base.py 定义能力协议）
    server.py       本地 HTTP 服务（标准库）
    ui.html         界面
    cli.py          命令行
  selftest.py       离线自测（不联网）
  data/             运行时生成：library/ inbox/ outbox/ config.json
```

## 新增一个平台

复制 `adapters/garmin.py`，改四件事：

```python
class MyMeter(Adapter):
    key = 'mymeter'          # 唯一标识
    name = '某某码表'
    caps = ('pull',)         # 真实能力，别多写
    note = '限制说明，直接显示给用户'

    def pull(self, limit=0, log=None) -> list[Ride]:
        ...
        return rides
```

然后在 `adapters/__init__.py` 的 `_INSTANCES` 里加一行。界面会自动出现。

## 自测

```bash
python selftest.py
```

覆盖：FIT 往返（点数/时间/里程/心率/功率/海拔/指纹）、GPX 往返、TCX 往返、
入库去重、文件夹与 zip 批量扫描、导出后可回读、OAuth1 签名（用规范的基串独立
验算）、RFC3986 编码、顽鹿参数、行者扫码的二维码内容与渲染、四方向能力矩阵。
全程不联网。

## 已知限制

- **Garmin 走 App 端接口写入**：官方 Connect API 没有「创建活动」，本项目用移动端
  SSO（账号+密码，必要时应答 MFA）+ `POST /upload-service/upload`，也就是官方 App
  自身同步活动用的那条链路。账号填**手机号**即可（中国站账号就是手机号，用户通常
  没设过邮箱；SSO 的 username 字段对手机号和邮箱一视同仁），国际站填邮箱。
  网页端 SSO 已加滑块验证码，不要走那条路。
  注意 garmin.cn 与 garmin.com 数据不互通，站点要在界面上选对。
- **行者只能用 App 扫码**：2026-11 起它的短信通道已关（`/api/v1/mobile/send_sms/`
  直接返回 `{"code":1,"sms_service_error":"发送短信失败"}`），只剩扫码。
  实现是逆向 H5 登录页得到的：`POST /api/scan_login/qrcode/` 拿 token（120 秒），
  二维码内容是 `imxingzhe://app.imxingzhe.com/login/confirm?token=...`，
  再用 `GET /api/scan_login/status/?token=...` 轮询，成功后直接给
  `access_token`/`refresh_token`。
- **iGPSPORT 读取靠两个接口**：`queryMyActivity` 拿列表，
  `getDownloadUrl/{rideId}` 拿 OSS 直链再下原始 FIT。
  ⚠️ `rideId` 必须写成**路径参数**，写 `?rideId=` 会被网关兜底成 500「服务器异常」；
  下载链接不带扩展名、Content-Type 是 octet-stream，只能靠文件头第 8~12 字节
  `b'.FIT'` 判断是不是 FIT。另外服务端每页最多给 20 条（传 30 也只给 20），
  分页必须看 `totalPage`，否则只读得到第一页 —— 去重会因此失效。
- **Garmin 读取要解 zip**：活动列表 `activitylist-service/activities/search/activities`，
  单条原始文件 `download-service/files/activity/{activityId}` 返回的是
  **zip 包**（`application/x-zip-compressed`），里面才是 `.fit`。
  手动录入/部分室内活动没有原始文件，会 404，跳过即可。
- **Wahoo 无法读取原始文件**：Cloud API 未公开活动文件下载接口。
- **行者和 iGPSPORT 用的是网页端自用接口**，不是官方合作通道，对方改版会失效
  （表现为 401/403 或字段缺失），需要跟着更新。
- **时长口径不同**：iGPSPORT 按起止跨度（含停顿）算，行者按骑行时间算，转换不改。
- **无 GPS 的室内记录**会被跳过（多数平台也拒收）。
- 去重指纹 = 开始时间(分钟) + 里程(10m 取整)，宁可重复不漏；重复可以手删。

## 隐私与配置

所有账号、密码、token、Cookie **只写在你自己电脑上的** `data/config.json`，
界面不会回显，也不会发到任何第三方服务器。可以设 `METERHUB_HOME` 改数据目录位置。

`data/` 已被 `.gitignore` 排除，仓库里只有 `config.example.json` 这个**全空字段示例**，
不含任何真实凭据。骑行记录（`data/library/*.fit`）、运行历史（`data/runs.jsonl`）、
日志都只留在本地。首次使用在网页界面里登录即可，不必手工编辑配置文件。

## 免责声明

本项目通过各平台网页/App 端自用接口读写**使用者本人**的数据，仅供个人数据迁移与学习研究。
它不是任何平台的官方工具，与 Garmin、Strava、Wahoo、行者、iGPSPORT、顽鹿（迈金）没有关联。
使用前请阅读并遵守各平台的服务条款；接口改版可能导致功能失效，风险由使用者自行承担。

## 许可证

[MIT](LICENSE)。第三方组件见 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。
