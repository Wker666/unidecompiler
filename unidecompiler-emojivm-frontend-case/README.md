# EmojiVM Frontend 实战案例

这个目录把一个真实的 EmojiVM 分析样本、参考运行程序、host runtime 和
`unidecompiler` 外部插件放在一起，作为可复现的逐步教程。

目录内容：

```text
unidecompiler-emojivm-frontend-case/
├── README.md
├── EMOJIVM.md
├── chal.evm
├── emojivm
├── runtime.py
└── unidecompiler-plugin-emojivm/
    ├── unidecompiler-plugin.toml
    └── emojivm_frontend/
```

`chal.evm` 是待分析的 EmojiVM 程序，`emojivm` 是参考执行器，
`runtime.py` 是 generic-IR simulator 使用的受信任 host 环境，插件目录则
负责识别、解码和 lifting。它们的职责不能混用。

这个案例现在还会把每条 EmojiVM 指令映射到原始 UTF-8 字节范围，
这样 GUI 的 bytecode/hex 联动可以直接定位到对应源码片段。

## 1. 准备工作

先进入本目录的父目录，并使用已经安装 `unidecompiler` 的虚拟环境：

```bash
cd /path/to/unidecompiler-emojivm-frontend-case
source /path/to/your/.venv/bin/activate
```

确认文件存在：

```bash
ls -l chal.evm emojivm runtime.py unidecompiler-plugin-emojivm
```

参考执行器不是 frontend。它只用于对照真实 VM 行为；反编译和模拟都通过
`unidecompiler` 的公共 API 完成。

## 2. 先读 VM 规范

阅读 [EMOJIVM.md](EMOJIVM.md)，先固定四件事：

1. 指令和数字 emoji 的映射；
2. 指令 offset 使用 Unicode codepoint index；
3. 栈操作的压入、弹出顺序；
4. `alloc/load/store/read/write/print` 的外部行为。

本案例的 frontend 在 `emojivm_frontend/model.py` 中保存这些解码事实，在
`decoder.py` 中把 UTF-8 文本转换成 `EmojiInstruction`，不会在 decoder 中
执行程序。

## 3. 检查插件边界

插件 manifest 是：

```toml
[frontend]
id = "emojivm"
module = "emojivm_frontend.plugin:EmojiVMFrontendPlugin"
```

`plugin.py` 只负责生命周期：

```text
bytes -> can_load()
bytes -> decode() -> FrontendModule(payload=EmojiVMProgram)
FrontendModule -> lift() -> ModuleIR
```

具体责任如下：

- `decoder.py`：UTF-8、opcode、数字 operand、offset 和 malformed 输入；
- `model.py`：frontend 私有的指令/程序模型；
- `lifter.py`：effects、调用形状、branch facts、region profile；
- `simulation.py`：只查找 generic-IR 中的 `main`，不执行 EmojiVM；
- `runtime.py`：只处理 generic IR 发出的外部调用和可变 buffer 状态。

## 4. 注册插件

### CLI/Python 注册

从本案例目录的父目录运行：

```python
from pathlib import Path

from unidecompiler.plugin_registry import FrontendRegistry

case_dir = Path("unidecompiler-emojivm-frontend-case").resolve()
registry = FrontendRegistry.discover()
plugin = registry.register_directory(case_dir / "unidecompiler-plugin-emojivm")
print(plugin.id, plugin.display_name, plugin.supported_inputs)
```

注册目录必须是**包含 `unidecompiler-plugin.toml` 的插件根目录**，不是
`emojivm_frontend/` 子目录。

### GUI 注册

1. 打开 Frontend manager；
2. 选择 Register folder；
3. 选择本目录下的 `unidecompiler-plugin-emojivm/`；
4. 确认列表中出现 `emojivm`；
5. 打开 `chal.evm`，确认它显示为 EmojiVM source，而不是 resource。

## 5. 第一次反编译 `chal.evm`

使用 CLI/API 读取文件，不要直接调用插件私有 decoder：

```python
from pathlib import Path

from unidecompiler import DecompilerEngine

case_dir = Path(".")
engine = DecompilerEngine.discover()
engine.register_frontend_directory(case_dir / "unidecompiler-plugin-emojivm")

result = engine.decompile_bytes(
    (case_dir / "chal.evm").read_bytes(),
    filename="chal.evm",
    frontend_id="emojivm",
)

print(result.status)
print(result.pseudocode.text if result.pseudocode else "<no pseudocode>")
print(result.diagnostics)
```

如果只想验证插件是否能被加载，可以运行：

```python
from pathlib import Path
from unidecompiler import DecompilerEngine

case_dir = Path("unidecompiler-emojivm-frontend-case")
engine = DecompilerEngine.discover()
engine.register_frontend_directory(case_dir / "unidecompiler-plugin-emojivm")
print([plugin.id for plugin in engine.registry.list()])
```

分析输出时重点看：

- 是否识别出 `function main()`；
- 是否出现 `alloc`、buffer 写入和 `write_buffer`；
- 是否出现 `read_buffer(1)`；
- 输入读取之后是否有 `load_byte`、比较和返回值；
- 复杂控制流是否以 `if/goto` 或低层 CFG 表示。

不要因为没有恢复成 `while` 就认为失败。首先检查 branch target、条件极性、
CFG edge 和 partial/unsupported 诊断是否正确。

## 6. 为什么需要 `runtime.py`

EmojiVM 的 generic IR 会把外部行为表示成命名调用，例如：

```text
alloc(...)
store_byte(...)
read_buffer(...)
load_byte(...)
write_buffer(...)
print(...)
```

共享 simulator 不知道这些名称的业务含义，因此需要 host environment。这个
案例的 `runtime.py` 导出同名 Python 函数：

| generic IR 名称 | runtime.py 行为 |
|---|---|
| `alloc` | 分配固定槽位的 byte buffer |
| `free` | 释放 buffer |
| `load_byte` | 读取一个字节 |
| `store_byte` | 写入低 8 位 |
| `read_buffer` | 从配置输入填充 buffer |
| `write_buffer` | 输出到捕获的 stdout |
| `puts_until_zero` | 输出单字节字符 |
| `print` | 输出十进制文本 |

它不是 EmojiVM 解释器，也不读取 `chal.evm`。它只维护一轮模拟所需的
host state。

## 7. 配置模拟输入

GUI 没有通用 stdin 控件时，runtime 使用环境变量：

```bash
export EMOJIVM_RUNTIME_STDIN='your-input'
```

然后从 GUI 的 Simulation 页面选择：

- Frontend：`emojivm`
- Target：`main`
- Runtime：本目录的 `runtime.py`

CLI 或测试可以通过标准输入提供数据；GUI 运行不要让 runtime 无条件阻塞读取
stdin。每次 Run 都应创建新 runtime 状态，避免 buffer 和输入游标跨运行泄漏。

## 8. 通过公共 simulator 运行

```python
from pathlib import Path

from unidecompiler.plugin_registry import FrontendRegistry
from unidecompiler_simulator import SimulationEngine
from unidecompiler_simulation_host_python import PythonFileEnvironment

case_dir = Path(".")
registry = FrontendRegistry.discover()
registry.register_directory(case_dir / "unidecompiler-plugin-emojivm")
simulator = SimulationEngine.from_registry(registry)

data = (case_dir / "chal.evm").read_bytes()
listing = simulator.list_artifact_targets(
    data,
    "chal.evm",
    frontend_id="emojivm",
)
print("targets:", listing.targets)
print("diagnostic:", listing.diagnostic)

result = simulator.simulate_artifact(
    data,
    "chal.evm",
    "main",
    frontend_id="emojivm",
    environment=PythonFileEnvironment.load(case_dir / "runtime.py"),
)
print("status:", result.status)
print("values:", result.values)
print("diagnostic:", result.diagnostic)
for event in result.events:
    if event.stdout or event.stderr or event.kind in {"external_call", "unsupported"}:
        print(event)
```

如果没有 runtime，或 runtime 没导出某个外部函数，正确结果是
`unsupported`/`raised`，不是伪造一个空返回值。

## 9. 对照参考执行器

`emojivm` 只用于验证 frontend/runtime 的语义假设：

```bash
chmod +x emojivm
printf '%s' 'your-input' | ./emojivm chal.evm
```

对照时记录：

1. 参考执行器的 stdout/stderr；
2. simulator 的 returned values；
3. simulator external-call events；
4. 最后执行的 block 和 step 数；
5. 两边是否在同一输入下产生相同的可观察输出。

参考执行器输出与 simulator 不一致时，优先检查：

- `load_byte`/`store_byte` 参数顺序；
- buffer index 和 offset 是否反了；
- `read_buffer` 是否只填充有效输入；
- `write_buffer` 的 NUL 终止规则；
- `print` 的返回值数量；
- branch condition 是 target-taken 还是 fallthrough 条件。

## 10. 分析 `chal.evm` 的正确顺序

不要从最后的巨大伪代码直接猜业务逻辑，按以下顺序缩小问题：

### 10.1 识别初始化阶段

大量 `alloc`、`store_byte`、`write_buffer` 通常是程序构造输出或初始化
buffer。先把它们按每次 `write_buffer` 分段。

### 10.2 找输入边界

在反编译结果中定位 `read_buffer(1)`。从这一点开始，后面的
`load_byte(...)` 通常是输入读取或输入检查。

### 10.3 标记状态来源

为每个值标记来源：

- 常量；
- 输入 buffer；
- 预先构造的 buffer；
- 比较结果；
- 外部调用返回值。

不要只看变量名；`order_tmp_*` 之类名字通常是 lifting 阶段的临时值。

### 10.4 还原检查条件

把 `load_byte` 的索引和值整理成表，再观察：

- 是否逐字节比较；
- 是否有长度检查；
- 是否有多个候选分支；
- 是否把输入转换后和常量比较。

### 10.5 用 trace 验证假设

先用空输入或短输入运行，记录第一个 `read_buffer` 后的事件；再逐步增加输入，
观察哪一个比较或分支发生变化。trace 只用于观察，不建立第二套解释器。

## 11. 已知限制

当前案例的插件是实验性 frontend。`chal.evm` 可能只能得到 partial recovery：

- 伪代码不一定结构化为 `while`；
- 复杂 CFG 可能保留 `if/goto`；
- 某些低层路径可能显示 unsupported；
- GUI CFG 必须能显示自环和异常边，但不应因某条边缺少 lane 而崩溃。

这些是分析结果状态，不能通过在 `runtime.py` 中伪造结果来掩盖。修复顺序应是：

1. 先验证 decoder offset；
2. 再验证 lifter effects；
3. 再验证 branch hints 和 stateful callbacks；
4. 最后验证 simulator/runtime 外部调用。

## 12. 修改代码后的验证顺序

每次修改都按这个顺序执行：

```bash
python -m py_compile \
  unidecompiler-plugin-emojivm/emojivm_frontend/*.py \
  runtime.py

```

测试文件属于本地开发验证，不随案例交付。当前 CLI 不接受临时插件目录参数，
因此外部插件的 CLI 验证使用第 5 节的 Python public API。GUI 和 CLI/脚本应始终
使用同一插件目录、同一 runtime 和同一 simulator public API。

## 13. 这个案例如何迁移到新的 VM

保留工程边界，不要复制 EmojiVM 的 opcode 名称：

1. 用新 VM 的规范替换 `EMOJIVM.md`；
2. 重写 `model.py` 和 `decoder.py`；
3. 按新 VM 的栈/寄存器语义重写 `lifter.py`；
4. 重新定义 branch target、条件极性和 offset 坐标系；
5. 只在 generic IR 需要外部行为时增加 runtime 函数；
6. 保持 simulator 负责 generic IR 执行；
7. 用参考实现和单元测试双重验证。

本目录的价值是展示完整工作流和分析方法，不是把 EmojiVM 的特殊规则当作
通用框架。
