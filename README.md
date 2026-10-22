# 望舒 Trading Lite

独立的单账户交易工程：小型执行内核、已跑通路径复用的 CTP 适配、原生 Qt UI、官方 vn.py CTA 策略。第一版集中完成人工与策略的交易闭环。

## 第一版保留什么

- CTP 认证、登录、当日结算确认、行情订阅、合约查询、资金和完整持仓查询。
- 人工买卖、开平、平今/平昨，LIMIT、FAK、FOK，选中撤单与全部撤单；受理、部分成交、成交、拒单、撤单状态以柜台回报为准。
- 期货，以及沿用买开/卖平范围的期权买方人工交易。期权卖方、主动行权和组合不在首版范围。
- 官方 `vnpy_ctastrategy.CtaTemplate` 与 `TargetPosTemplate` 的单标的 CTA：显式加载外部模块、默认参数、实例配置、初始化、启停、变量与错误展示；Tick、委托、成交回调；官方开平转换与本地停止单语义。
- 单一 `TradingKernel` 执行入口，人工与策略共用真实账户。策略 `pos` 是官方 CTA 对本策略已归属成交的跟踪值，不是独立资金账户，也不等同于柜台总持仓。
- 发送记录、请求去重、未知结果不自动重发、重启后的查询核对和同日匹配委托绑定。
- 明确的离线 PAPER 演练：合成报价、手动全部/部分成交、FAK/FOK 的 2 手模拟盘口、活动单和持仓持久化。

首版不包含本地资金预算、金额/持仓额度、亏损上限、报撤频率拦截、期权估值门槛、图表、指标管理、策略编辑器、回测平台、Web 服务或 Docker。价格步长、手数类型、交易所开平字段及连接状态校验仍保留；资金与可平量由柜台核验。原生 MARKET、旧工作台 LOCAL/SERVER 条件单页面不在 Lite 范围。

## 最小目录

```text
WangshuSystem-Trading-Lite/
├── wangshu_lite/
│   ├── models.py       # 冻结数据对象、网关协议
│   ├── core.py         # 校验 → 记录 → 一次发送 → 真实回报
│   ├── storage.py      # SQLite 意图、来源、审计、账户目录锁
│   ├── ctp.py          # 复用的 vn.py CTP 原生适配
│   ├── paper.py        # 离线柜台
│   ├── strategies.py   # 官方 CtaEngine 的轻量连接层
│   ├── history.py      # 显式 CSV 历史 K 线，供 CTA 初始化
│   ├── runtime.py      # 单账户装配、生命周期、公共操作
│   ├── ui.py           # Qt 展示与输入；无交易业务规则
│   └── __main__.py     # 桌面入口
├── tests/              # 全部离线，不连接柜台或加载用户策略
├── pyproject.toml
├── Start-Wangshu-Lite.cmd
└── start-mac.command
```

依赖方向：`UI / CTA → Runtime / TradingKernel → CTP 或 PAPER`，内核使用 `Journal`。内核本身只依赖 Python 标准库；Qt、CTP、CTA 按需安装。UI 与交易运行模块在同一应用内，耗时操作离开 Qt 主线程，CTA 回调在专属串行线程执行。界面正常退出会明确停止策略并关闭会话；不是驻留后台服务。

## 安装与打开

目标环境为 CPython 3.13 64 位。Windows 双击 `Start-Wangshu-Lite.cmd`；首次创建本工程自己的 `.venv` 并安装桌面、CTP 和 CTA 依赖。Mac 双击 `start-mac.command`，需预先安装 Python 3.13 和 Xcode Command Line Tools；Mac CTP 使用旧项目相同的官方固定兼容源码版本。

也可在本目录执行：

```sh
python3.13 -m venv .venv
# Windows 后续使用 .venv\Scripts\python.exe
.venv/bin/python -m pip install -e '.[desktop,ctp,strategy]'
.venv/bin/python -m wangshu_lite
```

仅离线人工交易 UI 可安装 `.[desktop]`；仅 Python 内核可直接 `pip install -e .`。策略依赖为官方 vn.py 4.4.0、CTA 1.4.1；CTP 版本与原项目保持一致。完整安装可能需要编译平台原生依赖；安装失败时启动器保留错误输出。

## 人工交易

1. 默认选择离线演练，点击连接。查看合成报价、资金、持仓和委托，再点击“允许下单”。
2. 在合约列表搜索并双击带入准确合约，或输入完整 `合约.交易所`。订阅后查看买卖报价；输入价格和手数，选择买卖、开平及 LIMIT/FAK/FOK，核对确认后提交。
3. 离线选中活动委托，可点击全部/部分成交，再撤销剩余部分。真实 CTP 模式没有模拟成交按钮。
4. 实际 CTP 使用券商确认的账户、BrokerID、AppID、AuthCode 和前置地址；登录前核对结算，并勾选登录确认当日结算。SDK 的“生产/测试”是原生 SDK 选择，不能证明账户是否为仿真。
5. “禁止新单”会停止策略并尝试撤本策略挂单；人工既有挂单继续以柜台状态为准。“全部撤单”逐笔请求撤销当前已知活动委托，不自动平仓。

所有数值控件禁止通过鼠标滚轮、触控板滚动或滑动改变数值。只能明确键盘输入或点击按钮。

## vn.py 策略交易

策略源码始终位于工程外。运行的应用只加载用户在文件选择器明确选择的受信任 `.py`，不自动扫描目录、不复制源码、不内置买卖算法、不在重启时自行导入或启动。外部 Python 代码拥有当前进程权限；这不是策略沙箱。

操作顺序：连接柜台 → 加载选定策略模块 → 选择类、实例名、准确合约和 JSON 参数 → 添加实例 → 按需导入历史 → 初始化 → 允许下单 → 单独启动策略。`BarGenerator`、`ArrayManager` 等沿用 vn.py 官方工具；实时 Tick 保留累计成交量、成交额与持仓量。

UI 文件入口适用于独立 `.py` 模块。带包相对导入的策略可先作为外部 Python 包安装，再由无 UI 的 Runtime 显式调用 `strategy("load_module", module_name="你的包.模块")`；同样不扫描目录、不复制源码。

策略的买开、卖开、卖平、买平由官方 CTA 语义处理；SHFE/INE 普通 `sell/cover` 使用官方 `OffsetConverter` 转换平今平昨，所有转换后的实际委托仍经 Lite 内核。启用数量以柜台实际持仓和回报为准，不创建策略资金账本。

停止单 `stop=True` 是官方 CTA 的本地停止单，只在应用和策略运行期间评估；停止策略、禁止新单或断线后取消等待中的本地停止单并尝试撤本策略活动委托。它不等于向柜台托管条件单。重启不恢复触发，不自动启动策略，也不自动补发旧信号。

参数、变量、委托归属和已处理成交标识保存在本机 Journal。重新连接后需要用户再次显式加载原外部模块，核对配置和柜台持仓，再初始化、启动。手工或其他终端的成交不会擅自计入某个策略的 `pos`。未核实发送记录会显示在“待核实”表，只允许与当前交易日已收到且字段匹配的柜台委托绑定；不提供清空未知记录按钮。

### 历史初始化

CTP 不提供历史 K 线。需要 `load_bar(days)` 的策略先通过策略页导入本地 CSV，选择准确合约和周期。CSV 为 UTF-8，可带 BOM；列如下，后三列可省略，时间未注明时区时按上海时区处理：

```csv
datetime,open,high,low,close,volume,turnover,open_interest
2026-10-22T09:00:00+08:00,3500,3502,3499,3501,100,3501000,10000
```

历史按合约、周期、时间保存在独立 `history.db`。`load_bar(days)` 只读取最近指定天数的已导入数据；没有匹配历史会明确初始化失败。实时合成周期由策略自己的官方 `BarGenerator` 完成。历史 Tick `load_tick`、邮件、组合/价差/多标的策略引擎和回测尚未接入，不做静默替代。

## 本机数据和保护

默认 Windows 数据目录为 `%LOCALAPPDATA%\WangshuLite`，Mac 为 `~/Library/Application Support/WangshuLite`；可通过 `--data-dir` 指定目录。PAPER 与 CTP 隔离，CTP 按 BrokerID、账户和明确选择的仿真/实盘环境绑定目录；切换主备前置继续使用同一份发送证据。账户环境须由用户按券商资料核对，不能靠 SDK 模式推断。CTP 流文件位于对应账户目录的 `.vntrader`，凭证仅在会话内存中，不写入工程配置。日志展示会脱敏。

PAPER 是固定合成交易日的流程演练，重启沿用保存的演练日；不模拟跨日结算、今仓转昨仓或真实撮合。发送结果未知而无可匹配柜台单、跨日才处置的情形，目前仍须保留证据停止核查，尚无跨日证据导入和结案接口。

同级 `../wangshu-protected/strategy/` 和 `../wangshu-protected/indicator/` 的保护约束与原项目一致。助手和测试不得进入或扫描；用户在应用中手动选择自己的文件不构成向助手开放源码的授权。源码、凭证、运行记录、数据库、流文件、虚拟环境及构建产物不进入 Git。

## 验证

```sh
python -m unittest discover -s tests -v
# 显式启用 Qt 离屏验收（须安装 desktop）
WANGSHU_LITE_QT_TESTS=1 QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
```

CTA 测试需安装 `.[strategy]`，Qt 测试需安装 `.[desktop]`；缺少可选依赖时相关测试明确跳过，不能算通过。测试使用临时目录、合成行情及无交易算法的回调测试替身，绝不连接 CTP 或加载受保护策略。

本版复用的是旧项目已跑通的 CTP 接入路径；独立 Lite 的柜台联调、Windows 双击安装和实机运行须在目标环境核验。离线与 UI 验收不等于真实柜台成交验收。实际本轮结果见 [VALIDATION.md](VALIDATION.md)。

CTA 接口依据：[官方 CTA 源码](https://github.com/vnpy/vnpy_ctastrategy)、[官方策略说明](https://github.com/vnpy/vnpy/blob/master/docs/community/app/cta_strategy.md)。
