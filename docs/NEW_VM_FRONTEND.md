# 编写新的 VM Frontend 完整指南

本文是 `unidecompiler` 新 VM/字节码 frontend 的实施手册。目标是：frontend 只提交 VM-neutral facts，core 负责栈恢复、CFG、结构化、AST、伪代码和诊断。

如果你正在给一个新 VM 写插件，按本文顺序做即可。不要从“先让 GUI 看起来像 C 代码”开始；先保证 decoder、effects、hints 和 CFG facts 完整、可验证。

如果你的 VM 不是栈机，先读第 29 节“选择你的 VM 建模路径”，再回来看 effects 和控制流模板。不要把栈机模板硬套到寄存器 VM 或三地址 VM。

如果你希望 frontend 支持可选的模拟执行，还必须阅读第 20.1 节及其后
的模拟验证清单。模拟支持不是把 frontend 变成解释器，而是为独立的
generic-IR simulator 提供数据化的函数查找和运行时事实。

## 0.1 先怎么读这份文档

如果你是第一次写 frontend，按这个顺序读：

- Day 1：第 0、2、3、4、5、7、8、11、12、13 节。
- Day 2：第 14、15、16、17、18、19、20、20.1 节。
- Day 3：第 23、23.1、24、25、27、28 节。
- Day 4：第 29、30、31、32、33、34、35、36、37 节。
- 最后：第 39 节，对照一个完整最小插件。

如果你只想先把第一个 frontend 跑起来，优先做第 0.3 节的 7 步，然后直接复制第 39.1 节的小插件再替换自己的 opcode 语义。

如果你的 VM 不是栈机，先读第 29 节，再决定第 8～18 节该用哪条模板。

## 0.2 这份文档里最重要的几个词

| 词 | 你可以把它理解成 |
|---|---|
| frontend | 只负责“看懂字节码事实”的适配层 |
| decoder | 把原始输入解析成稳定模型 |
| model | frontend 自己保留的私有解析结果 |
| step | 一条能提交给 core 的薄层指令事实 |
| effect | 这条指令对栈、变量、调用、返回做了什么 |
| hint | 这条指令告诉 core 的控制流/聚合/异常事实 |
| SourceRef | 这条事实来自哪一个原始位置 |
| target | 跳转要去的原始 offset |
| profile | core 用来恢复 CFG 的 opcode 分类表 |
| stateful callbacks | core 在复杂控制流下回调 frontend 的接口 |

最简单的心智模型是：

```text
decoder 负责“是什么”
effect 负责“做了什么”
hint 负责“往哪里去”
core 负责“把这些事实拼回结构”
```

## 0.3 最短上手路线

如果你现在就要开始写第一个 frontend，只做这 7 步：

1. 先选一个很小的 VM，只保留 3～5 个 opcode。
2. 写 `model.py`，只保留 `offset`、`opcode`、`size`、`operands`、`raw`。
3. 写 `decoder.py`，先让 `can_load()` 和 `decode()` 稳定。
4. 写 `plugin.py`，只负责注册和 `FrontendModule`。
5. 写 `lifter.py`，先支持 `CONST`、`ADD`、`RETURN`。
6. 再加一个 `JUMP` 和一个条件跳转。
7. 跑第 23～25 节的验证脚本，确认 GUI 和 CFG 都正常。

如果你想最快看到结果，直接复制第 39.1 节的小插件示例，再把 opcode 名和 operand 解码替换成你的 VM 语义。

## 0. 先明确 frontend 的边界

Frontend 可以做：

- 识别输入格式。
- 解析文件、函数、指令、常量、调试信息。
- 给每条指令生成 `VMBytecodeStep`。
- 给 step 填写 `decoded`、`raw`、`effects`、`hints`。
- 提供 `VMRegionOpcodeClasses`。
- 提供 `VMStatefulCallbacks`，让 core 能跨 basic block 保存 VM 栈和局部状态。
- 提交 branch target、loop backedge、case target、exception region 等中立事实。

Frontend 不可以做：

- 构造 `If`、`While`、`Switch`、`BasicBlock`、`FunctionIR`。
- 直接调用 `assemble_function`、`assemble_module`。
- 注册 CFG structurer。
- 在 backend/pseudocode 层补救控制流。
- 针对某个样本、fixture 或业务输入硬编码恢复规则。
- 把私有 decoder 对象塞进 core-visible operand、hint 或 metadata 里表达程序逻辑。
- 为 simulation 编写 VM bytecode interpreter、opcode 执行器或 frontend-specific
  frame/stack machine。
- 在 GUI、CLI 或 simulator 中实现自己的函数查找、重载选择或语言运行时语义。
- 让 `simulation_adapter` 返回 executable callback、frame、stack、decoder
  model 或不属于当前 lifted module 的函数。

关键判断标准：

```text
如果这个信息是“字节码事实”，frontend 可以提交。
如果这个信息是“源码结构”，必须交给 core 恢复。
```

例如：

- `offset 120 的 jnz 目标是 offset 80` 是事实，可以提交。
- `offset 80..120 是 while 循环` 是结构，frontend 不可以构造。

## 1. 总体数据流

```text
文件字节
  -> FrontendPlugin.can_load()
  -> FrontendPlugin.decode()
  -> FrontendModule(payload + metadata)
  -> frontend 转换为 VM 薄层事实
  -> VMBytecodeStep(decoded + raw + effects + hints)
  -> lift_vm_step_function()
  -> core 栈恢复 / CFG / region / SSA / AST
  -> DecompilerEngine 统一结果
  -> pseudocode backend / GUI / CLI
  -> [可选] generic IR simulator
  -> SimulationResult / trace
  -> CLI / GUI / embedding host
```

Frontend 私有的 decoder payload 只能在自己的包内使用。Core 可以接收 `FrontendModule.metadata`，但 metadata 只能是 provenance、diagnostics 和分析上下文，不能表达控制流决策。

## 2. 推荐目录结构

外部目录插件推荐这样放：

```text
my-vm-plugin/
├── unidecompiler-plugin.toml
├── README.md
├── pyproject.toml                  # 可选，作为 pip 包发布时需要
├── my_vm_frontend/
│   ├── __init__.py
│   ├── plugin.py                   # FrontendPlugin 门面
│   ├── decoder.py                  # 文件/字节码解析
│   ├── model.py                    # decoder 私有模型
│   ├── lifter.py                   # VMBytecodeStep/effects/hints
│   ├── simulation.py               # 可选：目标查找和数据化运行时适配
│   └── support.py                  # 可选，版本支持声明
└── tests/
    ├── test_decoder.py
    ├── test_lifter.py
    ├── test_simulation.py          # 声明支持模拟时必须有
    └── test_integration.py
```

如果使用 `src/` 布局：

```text
my-vm-plugin/
├── unidecompiler-plugin.toml
└── src/
    └── my_vm_frontend/
        ├── __init__.py
        ├── plugin.py
        ├── decoder.py
        ├── model.py
        └── lifter.py
```

GUI 注册外部目录时，传入插件根目录：

```text
/path/to/my-vm-plugin
```

## 3. 外部 manifest

插件根目录必须有：

```toml
# unidecompiler-plugin.toml
[frontend]
id = "my-vm"
module = "my_vm_frontend.plugin:MyVmFrontendPlugin"
```

规则：

- `id` 必须全局唯一、稳定。
- `module` 必须是 `python.module:attribute`。
- `attribute` 可以是 plugin 实例，也可以是零参数 plugin 类。
- 注册目录根或 `src/` 会被加入 Python import path。
- GUI 不会自动执行 `pip install`；第三方依赖要用户提前装好。

如果发布为 Python distribution，同时加 entry point：

```toml
[project.entry-points."unidecompiler.frontends"]
my-vm = "my_vm_frontend.plugin:MyVmFrontendPlugin"
```

entry point 由 `FrontendRegistry.discover()` 自动发现。外部 manifest 由宿主显式注册。

## 4. FrontendPlugin 门面

最小 plugin：

```python
from __future__ import annotations

from unidecompiler.core.ir import ModuleIR
from unidecompiler.plugins import FrontendDecodeError, FrontendModule, FrontendVersionSupport

from .decoder import decode_my_vm, looks_like_my_vm
from .lifter import lift_program


class MyVmFrontendPlugin:
    id = "my-vm"
    display_name = "My VM"
    supported_inputs = (".mvm", ".mvmc")
    version_support = FrontendVersionSupport(
        family="my-vm-bytecode",
        versions=("1",),
        parser="my-vm parser 1.0",
        status="experimental",
        notes=("Initial VM frontend.",),
    )

    def can_load(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_my_vm(data, filename)

    def decode(self, data: bytes, filename: str | None = None) -> FrontendModule:
        try:
            program = decode_my_vm(data, filename)
        except ValueError as error:
            raise FrontendDecodeError(str(error)) from error
        return FrontendModule(
            frontend_id=self.id,
            payload=program,
            metadata={
                "filename": filename,
                "format": "my-vm",
                "version": program.version,
                "endianness": program.endianness,
                "debug_info_present": bool(program.debug_lines),
                "my-vm": {
                    "function_count": len(program.functions),
                    "instruction_count": sum(len(f.instructions) for f in program.functions),
                },
            },
        )

    def lift(self, module: FrontendModule) -> ModuleIR:
        if module.frontend_id != self.id:
            raise TypeError(f"wrong frontend module: {module.frontend_id!r}")
        return lift_program(module.payload, module.metadata)
```

`can_load()` 要快速、无副作用、不能执行外部程序。多个 frontend 同时返回 true 时，engine 会报告 ambiguous，而不是猜。

`decode()` 只做格式解析。它可以返回 frontend 私有模型，但不能构造 core IR。

`lift()` 只把私有模型转换成 thin VM facts，再调用 core helper。

## 5. Decoder 模型

Decoder 的职责是把输入变成稳定的 frontend 私有模型。

推荐模型：

```python
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MyInstruction:
    offset: int
    opcode: str
    size: int
    operands: tuple[Any, ...] = ()
    raw: str = ""
    line: int | None = None


@dataclass(frozen=True)
class MyFunction:
    name: str
    offset: int
    instructions: tuple[MyInstruction, ...]
    params: tuple[str, ...] = ()
    local_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class MyProgram:
    filename: str | None
    version: str
    endianness: str | None
    functions: tuple[MyFunction, ...]
    debug_lines: dict[int, int]
    diagnostics: tuple[str, ...] = ()
    word_size: int | None = None
```

### 平坦 VM 程序

有些 VM 没有函数表，整个文件就是一段平坦指令流。

这时 frontend 应把整段程序包装成一个入口函数：

```python
main = MyFunction(
    name="main",
    offset=instructions[0].offset,
    instructions=tuple(instructions),
    params=(),
    local_names=(),
)
```

不要因为没有函数表就跳过 `VMFunctionSpec`。Core 的恢复入口仍然是函数。

### 文本 VM 与空白字符

文本 VM 的解析必须跟解释器保持一致。

如果解释器允许空白分隔，decoder 可以跳过 whitespace。如果解释器逐字符取指并把未知字符视为错误，decoder 也应该把 whitespace 视为错误。

不要为了让样本“更容易解析”而宽松忽略字符。Frontend 的 decoder 是格式事实来源，应尽量匹配真实 VM。

Decoder 必须保留：

- 原始 bytecode offset。
- opcode 名称。
- operand 原始值。
- raw 反汇编文本。
- 函数边界。
- 可用的 locals、参数名、常量名、debug line。
- malformed 输入的错误位置。

错误处理：

- 输入明显不是该格式：`can_load()` 返回 `False`。
- 输入像该格式但损坏：`decode()` 抛 `FrontendDecodeError`。
- 未知 opcode：能确定边界时仍生成 instruction，并在 lift 时用 `UnknownOpcode`；不能确定边界时抛 decode error。

## 6. SourceRef 与 offset 单位

每个可定位事实都应该带 `SourceRef`：

```python
from unidecompiler.core.ir import SourceRef

source = SourceRef(
    frontend="my-vm",
    offset=instruction.offset,
    line=instruction.line,
    detail=f"function={function.name}",
)
```

字段规则：

- `frontend`：必须等于 plugin id。
- `offset`：原始 VM 指令位置，不是伪代码行号，也不是 instruction index。
- `line`：VM debug/source line，没有就 `None`。
- `detail`：只放 provenance，例如函数名、段名、方法签名。

`offset` 的单位由 VM 格式决定，但必须和 branch target 使用同一坐标系。

常见选择：

- 二进制 bytecode：使用文件内 byte offset。
- 容器内函数 bytecode：使用函数 code 区内 byte offset，或全局 byte offset，但必须全程一致。
- 文本 VM：使用解释器实际使用的字符位置。
- Unicode 文本 VM：如果解释器按 Unicode codepoint 取指，就用 codepoint index；不要用 UTF-8 byte offset。

如果一条指令占多个输入单位，`SourceRef.offset` 应指向 opcode 的位置，不是 operand 的位置。Branch target 也必须落在 opcode offset 上。

例如一个 Unicode 文本 VM：

```text
OP ARG
```

如果 `OP ARG` 占两个 Unicode codepoints，指令 offset 是 `OP` 的 codepoint index，`ARG` 是 operand，不是合法跳转目标。

验证 target 时应该检查：

```python
valid_offsets = {instruction.offset for instruction in instructions}
assert target in valid_offsets
```

不要把控制流决策放进 `SourceRef.detail` 或 metadata。控制流事实必须用 `VMHint`。

推荐 module metadata：

```python
metadata={
    "filename": filename,
    "format": FRONTEND_ID,
    "version": program.version,
    "endianness": program.endianness,
    "debug_info_present": bool(program.debug_lines),
    "diagnostics": tuple(program.diagnostics),
    FRONTEND_ID: {
        "word_size": program.word_size,
        "function_count": len(program.functions),
    },
}
```

## 7. VMOperand 与 VMDecodedInstruction

`VMDecodedInstruction` 是 GUI/CLI 可展示的中立反汇编行，不执行语义。

```python
from unidecompiler.core.vm_operands import VMDecodedInstruction, VMOperand

decoded = VMDecodedInstruction(
    opcode=instruction.opcode,
    source=source,
    operands=(
        VMOperand(role="constant", value=3, text="const[3]"),
        VMOperand(role="target", value=120, text="0x78"),
    ),
    raw=instruction.raw,
)
```

常用 `VMOperand.role`：

| role | 用途 |
|---|---|
| `constant` | 常量索引或已解析常量 |
| `local` | 局部变量槽位或名称 |
| `global` | 全局变量标识 |
| `register` | VM 寄存器 |
| `target` | branch/switch 目标 offset |
| `attribute` | 属性名 |
| `member` | 成员/字段名 |
| `immediate` | 数字、mode、宽度等立即数 |
| `raw` | 无法归类但仍要展示的 operand |

规则：

- `value` 用稳定、可序列化的中立值。
- `text` 用于展示。
- 不要把 decoder 私有对象放进 operand。
- branch target 使用 `role="target"`。
- opcode 没有 operand 时传空 tuple。

## 8. Effect：描述栈和值行为

Effect 是 core 能执行的最小 VM 语义。Frontend 选择 effect，不直接操作 `StackMachineState`。

常用类别：

| 类别 | Effect 示例 | 用途 |
|---|---|---|
| 值/栈 | `Push`, `Pop`, `Copy`, `DuplicateTop`, `Swap`, `Unpack` | 栈形状和常量值 |
| 局部变量 | `LoadLocal`, `StoreLocal`, `AssignValue`, `UpdateLocal`, `StoreMany` | locals |
| 运算 | `Unary`, `Binary`, `Compare`, `Truthy`, `SelectValue` | 表达式和条件 |
| 属性/索引 | `LoadAttr`, `StoreAttr`, `LoadItem`, `StoreItemEffect`, `LoadIndirect` | member/index |
| 容器 | `BuildArray`, `BuildSet`, `BuildMap`, `BuildString` | aggregate |
| 调用 | `Invoke`, `BuildCall`, `CallStackArgs` | 调用 |
| 终止 | `ReturnTop`, `ReturnVoid`, `RaiseTop`, `YieldTop` | terminator |
| 回退 | `UnknownOpcode` | 可诊断 unsupported |

Effect table 示例：

```python
from unidecompiler.core.effects import Binary, LoadLocal, Push, ReturnTop, StoreLocal, UnknownOpcode
from unidecompiler.core.ir import Const
from unidecompiler.core.vm_effect_table import VMEffectTable


MY_EFFECT_TABLE = VMEffectTable(
    opcode_attr="opcode",
    exact={
        "CONST": lambda ctx, ins, src: (
            Push(source=src, value=Const(source=src, value=ctx.constants[ins.operands[0]])),
        ),
        "LOAD_LOCAL": lambda ctx, ins, src: (
            LoadLocal(source=src, name=ctx.local_names[ins.operands[0]]),
        ),
        "STORE_LOCAL": lambda ctx, ins, src: (
            StoreLocal(source=src, name=ctx.local_names[ins.operands[0]]),
        ),
        "ADD": lambda ctx, ins, src: (
            Binary(source=src, op="+", semantics="static"),
        ),
        "RETURN": lambda ctx, ins, src: (
            ReturnTop(source=src),
        ),
    },
    fallback=lambda ctx, ins, src: (
        UnknownOpcode(source=src, opcode=ins.opcode, raw=ins.raw),
    ),
)
```

未知 opcode 不要返回空 tuple。空 tuple 只适合明确无语义的 noise opcode，比如 `nop`、padding、line marker。

### Opcode 映射表模板

给新 VM 写 frontend 时，先填一张表，再写代码。

| VM opcode | operands | 栈输入 | 栈输出 | effect | hints | 测试 |
|---|---|---|---|---|---|---|
| `CONST` | const id | 无 | value | `Push(Const(...))` | 无 | 常量值正确 |
| `LOAD_LOCAL` | slot | 无 | local | `LoadLocal` | 无 | local 名正确 |
| `STORE_LOCAL` | slot | value | 无 | `StoreLocal` | 无 | 赋值目标正确 |
| `ADD` | 无 | left, right | result | `Binary("+")` | 无 | 操作数顺序 |
| `CALL` | argc | args | return(s) | `Invoke`/`CallStackArgs` | `call-shape` 可选 | 参数数量 |
| `JUMP` | target 或栈值 | target 可选 | 无 | target 在栈上时 `Pop` | `branch-target`/`loop-backedge` | target 存在 |
| `JUMP_IF_*` | target 或栈值 | condition/target | 无 | 通常不提前 pop | `branch-target`、`materialized-condition` | if/goto |
| `RETURN` | 无 | value 可选 | 函数结束 | `ReturnTop`/`ReturnVoid` | 无 | terminator |
| `NOP` | 无 | 无 | 无 | `()` | 可选 `noise` | 不产生语句 |

这张表应由 VM 规范或解释器实现驱动，不由某个样本的反编译结果驱动。

每行至少回答：

- opcode 是否改变栈深度？
- 操作数顺序是否和 core 默认一致？
- 是否可能结束函数？
- 是否产生控制流 target？
- target 是 immediate、table entry、还是栈值？
- 条件是否已经物化在栈上？
- 是否需要 runtime call 名称表达副作用？

## 9. 栈操作数顺序

栈机 frontend 最容易错的是二元操作顺序。

Core 的栈约定：

```text
stack[-1] 是栈顶
stack[0] 是当前可见栈片段的最底部
```

假设运行时语义是：

```text
right = pop()
left = pop()
push(left OP right)
```

那 effect 通常可以直接用：

```python
Binary(source=source, op="+")
```

这时：

```text
const 5; const 2; sub
```

应该恢复为：

```text
5 - 2
```

如果 VM 语义是“栈顶作为左操作数”：

```text
left = pop()
right = pop()
push(left OP right)
```

那对非交换操作必须先交换：

```python
from unidecompiler.core.effects import Binary, Swap

if opcode in {"SUB", "MOD", "LT"}:
    return (
        Swap(source=source, depth=2),
        Binary(source=source, op=op, semantics="static"),
    )
```

必须为这些 opcode 写小样例：

```text
const 5; const 2; sub; print
const 5; const 2; mod; print
const 2; const 5; lt; print
```

如果这个 VM 是“栈顶作为左操作数”，验证伪代码分别应类似：

```text
print(2 - 5)
print(2 % 5)
print(5 < 2)
```

如果你看到 `5 - 2`、`5 % 2`、`2 < 5`，说明你按默认 `left=below, right=top` 建模了。两种语义都可能存在，必须以目标 VM 解释器或格式文档为准。

## 10. 调用、内存和 VM runtime API

如果 VM opcode 调用 runtime API，可以用 `CallStackArgs`：

```python
from unidecompiler.core.effects import CallStackArgs

if opcode == "READ":
    return (CallStackArgs(source=source, callee_name="read_buffer", arg_count=1, returns=0),)

if opcode == "LOAD_BYTE":
    return (CallStackArgs(source=source, callee_name="load_byte", arg_count=2),)

if opcode == "STORE_BYTE":
    return (CallStackArgs(source=source, callee_name="store_byte", arg_count=3, returns=0),)
```

注意参数顺序要和 VM 运行时一致。比如如果 bytecode 栈上顺序是：

```text
const index
const offset
STORE
```

而你希望输出：

```c
store_byte(index, offset, value)
```

就必须用 `Swap` 或调整 effect，保证 core 看到的参数顺序正确。

运行时调用名应稳定、可读、VM-neutral。不要把某个样本的业务语义写成 `validate_domain_rule()` 这种名称，除非 VM 本身 opcode 就是这个含义。

### 固定参数 API

如果 opcode 消费固定数量的栈参数，优先用 `CallStackArgs`。

例如：

```text
READ    消费 buffer_index
WRITE   消费 buffer_index
LOAD    消费 index, offset，返回 byte
STORE   消费 index, offset, value，不返回
```

对应可表示为：

```python
CallStackArgs(source=source, callee_name="read_buffer", arg_count=1, returns=0)
CallStackArgs(source=source, callee_name="write_buffer", arg_count=1, returns=0)
CallStackArgs(source=source, callee_name="load_byte", arg_count=2, returns=1)
CallStackArgs(source=source, callee_name="store_byte", arg_count=3, returns=0)
```

如果参数顺序不匹配，先用已有 stack effect 调整，例如 `Swap`。如果现有 effect 不能无损表达该重排，应考虑新增 VM-neutral effect，或保守降级为 partial/unsupported。

不要通过改函数名来掩盖顺序错误。

### 动态参数 API

有些 opcode 消费数量不是固定值。例如 `PRINT_UNTIL_ZERO` 可能持续 pop，直到遇到 0 sentinel。

如果 core 还没有对应的 VM-neutral effect，推荐顺序是：

1. 如果该 opcode 不影响后续 stack merge，可用保守 runtime call 名称表达，例如 `puts_until_zero(...)`。
2. 如果会影响后续栈状态，返回 `effects=None` 或 `UnknownOpcode`，让 core 给出 partial/unsupported。
3. 如果这是多 VM 都会遇到的模式，应新增 VM-neutral effect，再由 core 支持。

不要在 frontend 里手动弹栈到 sentinel 并拼字符串。那是对运行时数据的特例解释，不是 thin IR fact。

### 隐式返回和隐式状态

有些 opcode 修改 VM 内部状态，但不把结果压回栈。

例如 allocator：

```text
ALLOC_BUFFER size
```

如果 VM 运行时把新 buffer 放进第一个空槽，但 opcode 不返回槽位，那么可以先表达成：

```python
CallStackArgs(source=source, callee_name="alloc", arg_count=1, returns=0)
```

这保留了副作用调用，但不会假装知道返回值。

如果后续分析必须知道“分配到了哪个槽”，这已经超出普通 runtime call 的表达能力。应考虑新增 VM-neutral memory/allocation fact，或接受 partial/低层输出。

## 11. 控制流总原则

控制流恢复依赖三类信息：

1. opcode 分类：这是 jump、conditional jump、return、noise 还是普通指令。
2. target hint：跳转目标 offset 是多少。
3. 条件栈状态：core 在生成 `Branch` 时能否拿到条件表达式。

Frontend 不构造 CFG，但必须把这三类事实提交完整。

最低合格结果：

```text
简单控制流 -> core 输出 if/while/switch
复杂控制流 -> core 输出低层 if/goto CFG
无法安全恢复 -> explicit partial/unsupported + raw context
```

最坏情况也不应该 silently 线性化复杂控制流。线性化会误导分析。

## 12. VMHint：branch、loop、switch、exception

`VMHint` 只提交事实：

```python
from unidecompiler.core.vm_hints import VMHint

VMHint(kind="branch-target", source=source, target=target, flow="conditional")
VMHint(kind="loop-backedge", source=source, target=header_offset, flow="conditional")
VMHint(kind="case-target", source=source, target=case_offset, value=case_value)
VMHint(kind="default-target", source=source, target=default_offset)
VMHint(kind="exception-region", source=source, value={"start": start, "end": end, "target": handler})
```

合法 kind：

```text
block-boundary
branch-target
case-target
default-target
fallthrough
loop-backedge
exception-region
exception-handler
branch-value
materialized-condition
call-shape
aggregate-shape
```

### branch-target 与 loop-backedge

通常规则：

```python
kind = "loop-backedge" if target <= instruction.offset else "branch-target"
```

后向边用 `loop-backedge`，前向边用 `branch-target`。这不会构造 while，只是告诉 core 这条边是后向控制流事实。

### 条件极性

`detail` 可以说明 target 是 true 边还是 false 边：

```python
VMHint(
    kind="branch-target",
    source=source,
    target=target,
    flow="conditional",
    detail="target-if-true",
)
```

如果 `branch_condition()` 返回“跳转目标被采用时为真”的表达式，必须用：

```text
detail="target-if-true"
```

如果 `branch_condition()` 返回“fallthrough 时为真”的表达式，可以使用默认极性，或明确写：

```text
detail="target-if-false"
```

极性错了，伪代码里的 `if` 分支会反，失败路径和成功路径可能被读反。

### materialized-condition

如果 VM 的条件是先在栈上算出来，然后由 `jz/jnz` 消费，例如：

```text
LOAD x
CONST 0
EQ
CONST target
JUMP_IF_TRUE
```

应该给 `JUMP_IF_TRUE` 同时提交：

```python
VMHint(kind="materialized-condition", source=source, detail="stack", flow="conditional")
```

这个 hint 的含义是：条件已经作为栈值物化，core 应按 stack condition 恢复控制流。

没有这个 hint 时，core 可能把条件计算当普通线性表达式，复杂函数可能最终看不到 `if/goto`。

## 13. 条件跳转的正确写法

这是栈机 frontend 最重要的部分。

先确认 target 从哪里来。Immediate target 和 stack target 的写法不同。

```text
# immediate target
... condition
JUMP_IF_TRUE target

# stack target
... condition target
JUMP_IF_TRUE
```

如果 target 是 immediate，branch 通常只从栈上消费 condition。如果 target 在栈上，branch 通常同时消费 condition 和 target。

`branch_condition(branch, stack)` 收到的是即将被 branch 消费的栈片段，不是完整 VM 栈。

约定：

```text
stack[-1] 是该片段的栈顶
stack[0] 是该片段中最早压入的值
```

如果运行时布局是：

```text
... condition target
JUMP_IF_TRUE
```

且 `branch_stack_width()` 返回 `2`，那么 callback 收到：

```text
stack == (condition, target)
stack[0] == condition
stack[-1] == target
```

如果运行时布局是：

```text
... target condition
JUMP_IF_TRUE
```

那么 callback 收到：

```text
stack == (target, condition)
stack[0] == target
stack[-1] == condition
```

所以文档示例不能机械复制。必须先写清楚目标 VM 的 branch 栈布局，再决定从 `stack[0]` 还是 `stack[-1]` 取 condition。

### immediate target 模板

如果运行时语义是：

```text
condition = pop()
if condition != 0:
    pc = instruction.target
```

control effects 通常不需要弹栈。Core 会根据 `branch_stack_width` 在控制流边上消费 condition。

```python
from unidecompiler.core.ir import BinaryOp, Const


CONTROL = frozenset({"JUMP", "JUMP_IF_TRUE", "JUMP_IF_FALSE"})


def effects_for_control(instruction, source):
    if instruction.opcode in CONTROL:
        return ()


def branch_stack_width(instruction):
    if instruction.opcode in {"JUMP_IF_TRUE", "JUMP_IF_FALSE"}:
        return 1
    return 0
```

`branch_condition()`：

```python
def branch_condition(branch, stack):
    if len(stack) < 1:
        return None
    condition = stack[-1]
    if branch.opcode == "JUMP_IF_TRUE":
        return BinaryOp(source=condition.source, op="!=", left=condition, right=Const(value=0, source=condition.source))
    if branch.opcode == "JUMP_IF_FALSE":
        return BinaryOp(source=condition.source, op="==", left=condition, right=Const(value=0, source=condition.source))
    return None
```

### stack target 模板

如果运行时语义是：

```text
target = pop()
condition = pop()
if condition != 0:
    pc = target
```

无条件 jump 可以在 effect 阶段弹掉栈顶 target；条件 jump 不要在 effect 阶段提前弹 condition/target。

这里的职责要分清：

- `Pop` 是线性 lift 阶段对 VM stack 的语义建模。
- `branch_stack_width` 是 stateful control recovery 在分支边上需要从当前 stack state 取出/移除多少个控制值。
- immediate target jump 不消费栈上 target，所以两者通常都不为 target 额外消费栈值。
- stack target jump 的 target 本来就在栈上，所以必须明确由哪一层消费，不能既在普通 effect 里消费，又在同一条路径里重复消费。

```python
from unidecompiler.core.effects import Pop
from unidecompiler.core.ir import BinaryOp, Const


CONTROL = frozenset({"JUMP", "JUMP_IF_TRUE", "JUMP_IF_FALSE"})


def effects_for_control(instruction, source):
    if instruction.opcode == "JUMP":
        # 仅适用于 target 位于栈顶的无条件跳转。
        return (Pop(source=source),)
    if instruction.opcode in {"JUMP_IF_TRUE", "JUMP_IF_FALSE"}:
        # 不在 effect 阶段 pop 条件/目标。
        # branch_stack_width 会告诉 core 分支消费几个栈值。
        return ()


def branch_stack_width(instruction):
    if instruction.opcode == "JUMP":
        return 1
    if instruction.opcode in {"JUMP_IF_TRUE", "JUMP_IF_FALSE"}:
        return 2
    return 0
```

`branch_condition()`：

```python
def branch_condition(branch, stack):
    if len(stack) < 2:
        return None
    # 本模板假设运行时布局是: ... condition target
    # 因此 branch_stack_width=2 时，stack == (condition, target)。
    condition = stack[0]
    if branch.opcode == "JUMP_IF_TRUE":
        return BinaryOp(source=condition.source, op="!=", left=condition, right=Const(value=0, source=condition.source))
    if branch.opcode == "JUMP_IF_FALSE":
        return BinaryOp(source=condition.source, op="==", left=condition, right=Const(value=0, source=condition.source))
    return None
```

注意这里返回的是 target-taken condition。即使 opcode 名叫 `JUMP_IF_FALSE`，只要返回 `condition == 0`，它就表示“表达式为真时跳到 target”。

把 callback 接到 core：

```python

stateful_callbacks=VMStatefulCallbacks(
    initial_locals=lambda: {},
    lift_linear=lift_linear,
    branch_condition=branch_condition,
    branch_stack_width=branch_stack_width,
)
```

并在 step 上提交：

```python
hints = (
    VMHint(
        kind="loop-backedge" if target <= instruction.offset else "branch-target",
        source=source,
        target=target,
        label=instruction.opcode,
        flow="conditional",
        detail="target-if-true",
    ),
    VMHint(
        kind="materialized-condition",
        source=source,
        label=instruction.opcode,
        detail="stack",
        flow="conditional",
    ),
)
```

这个模式会让 core 至少能生成低层 CFG：

```c
if (condition) goto block_target else goto block_fallthrough
```

如果函数结构简单，core 可能进一步恢复为 `if` 或 `while`。如果结构复杂，保守的 `if/goto` 是正确输出。

### 条件跳转检查清单

每个条件跳转 opcode 都要回答：

- target 是 immediate，还是栈值？
- condition 是 immediate mode/marker，还是栈值？
- runtime 消费几个栈值？
- `branch_stack_width` 是否等于 core 需要移除的值数？
- `branch_condition()` 使用的是正确的栈位置吗？
- `branch_condition()` 返回的是 target 成立条件，还是 fallthrough 成立条件？
- 是否需要 `detail="target-if-true"` 或 `detail="target-if-false"`？
- 条件是否已经物化在栈上，是否需要 `materialized-condition`？
- 后向 target 是否用 `loop-backedge`？

如果其中任一项不清楚，先写最小控制流样例验证，不要直接上真实大样本。

## 14. 计算静态跳转目标

有些 VM 的跳转目标是 immediate operand，有些是栈上或寄存器里算出的常量。不同来源用不同 resolver。

Immediate absolute target：

```python
CONTROL_WITH_IMMEDIATE_TARGET = frozenset({"JUMP", "JUMP_IF_TRUE", "JUMP_IF_FALSE"})


def immediate_absolute_targets(function) -> dict[int, int]:
    targets: dict[int, int] = {}
    valid_offsets = {ins.offset for ins in function.instructions}
    for ins in function.instructions:
        if ins.opcode not in CONTROL_WITH_IMMEDIATE_TARGET:
            continue
        target = int(ins.operands[0])
        if target in valid_offsets:
            targets[ins.offset] = target
    return targets
```

Immediate relative target：

```python
CONTROL_WITH_RELATIVE_TARGET = frozenset({"JUMP_REL", "JUMP_IF_TRUE_REL", "JUMP_IF_FALSE_REL"})


def immediate_relative_targets(function) -> dict[int, int]:
    targets: dict[int, int] = {}
    valid_offsets = {ins.offset for ins in function.instructions}
    for ins in function.instructions:
        if ins.opcode not in CONTROL_WITH_RELATIVE_TARGET:
            continue
        delta = int(ins.operands[0])
        target = ins.offset + ins.size + delta
        if target in valid_offsets:
            targets[ins.offset] = target
    return targets
```

Stack target 需要常量传播。

例如：

```text
CONST 7
CONST 10
MUL
CONST 5
ADD
JUMP
```

目标是 `75`。Frontend 可以做局部常量传播，恢复 target hint。

stack-target VM 的保守 resolver 示例：

```python
def const_binary(opcode: str, left: int | None, right: int | None) -> int | None:
    if left is None or right is None:
        return None
    if opcode == "ADD":
        return left + right
    if opcode == "SUB":
        return left - right
    if opcode == "MUL":
        return left * right
    if opcode == "MOD":
        return left % right
    if opcode == "XOR":
        return left ^ right
    if opcode == "AND":
        return left & right
    if opcode == "EQ":
        return int(left == right)
    if opcode == "LT":
        return int(left < right)
    return None


def pop_many(stack: list[int | None], count: int) -> None:
    for _ in range(count):
        if stack:
            stack.pop()


def apply_conservative_stack_effect(stack: list[int | None], ins) -> None:
    if ins.opcode == "NOP":
        return
    if ins.opcode == "DROP":
        pop_many(stack, 1)
        return
    if ins.opcode == "DUP":
        stack.append(stack[-1] if stack else None)
        return
    stack.clear()


def static_branch_targets(function) -> dict[int, int]:
    targets: dict[int, int] = {}
    stack: list[int | None] = []
    valid_offsets = {ins.offset for ins in function.instructions}

    for ins in function.instructions:
        if ins.opcode == "CONST":
            stack.append(int(ins.operands[0]))
        elif ins.opcode in {"ADD", "SUB", "MUL", "MOD", "XOR", "AND", "EQ", "LT"}:
            right = stack.pop() if stack else None
            left = stack.pop() if stack else None
            stack.append(const_binary(ins.opcode, left, right))
        elif ins.opcode == "JUMP":
            target = stack[-1] if stack else None
            if target in valid_offsets:
                targets[ins.offset] = target
            pop_many(stack, 1)
        elif ins.opcode in {"JUMP_IF_TRUE", "JUMP_IF_FALSE"}:
            target = stack[-1] if stack else None
            if target in valid_offsets:
                targets[ins.offset] = target
            pop_many(stack, 2)
        else:
            apply_conservative_stack_effect(stack, ins)

    return targets
```

规则：

- 只提交能确定且落在真实 instruction offset 上的 target。
- 算不出来就不要猜。
- 无效 target 应作为 diagnostics 或 unsupported context 暴露。
- target 是原始 bytecode offset，不是 instruction index。

## 15. Opcode 分类与 region profile

需要控制流恢复时，提交 `VMRegionOpcodeClasses`：

```python
from unidecompiler.core.vm_region import VMRegionOpcodeClasses, build_hint_region_profile

classes = VMRegionOpcodeClasses(
    noise=frozenset({"NOP"}),
    control=frozenset({"JUMP", "JUMP_IF_TRUE", "JUMP_IF_FALSE"}),
    jumps=frozenset({"JUMP", "JUMP_IF_TRUE", "JUMP_IF_FALSE"}),
    forward_jumps=frozenset({"JUMP", "JUMP_IF_TRUE", "JUMP_IF_FALSE"}),
    backward_jumps=frozenset({"JUMP", "JUMP_IF_TRUE", "JUMP_IF_FALSE"}),
    conditional_jumps=frozenset({"JUMP_IF_TRUE", "JUMP_IF_FALSE"}),
)

profile = build_hint_region_profile(
    steps,
    frontend="my-vm",
    opcode_classes=classes,
    raw_window=lambda index: raw_window(instructions, index),
)
```

如果同一个 opcode 既可能前跳也可能后跳，可以同时列在 `forward_jumps` 和 `backward_jumps`，具体方向由 hint target 与 source offset 决定。

`noise` 只放真正无语义指令。不要把失败解析的 opcode 放进 noise。

## 16. lift_linear 与 stateful callbacks

复杂控制流需要 `stateful_callbacks`。

模板：

```python
from unidecompiler.core.vm_function import lift_steps
from unidecompiler.core.vm_region import VMLinearState, VMStatefulCallbacks


def lift_linear(program, function, start, end, locals_, stack):
    steps = tuple(make_step(program, ins) for ins in function.instructions[start:end])
    result = lift_steps(
        steps,
        initial_locals=locals_,
        initial_stack=stack,
    )
    stopped_at = None
    if result.stopped_at is not None:
        stopped_at = start + steps.index(result.stopped_at)
    return VMLinearState(
        locals=result.state.locals,
        stack=tuple(result.state.stack),
        statements=tuple(result.state.statements),
        terminator=result.state.terminator,
        stopped_at=stopped_at,
    )


def make_stateful_callbacks(program, function):
    return VMStatefulCallbacks(
        initial_locals=lambda: {},
        lift_linear=lambda start, end, locals_, stack: lift_linear(program, function, start, end, locals_, stack),
        branch_condition=branch_condition,
        branch_stack_width=branch_stack_width,
    )
```

`lift_linear` 只解释 `[start, end)` 的线性指令 slice。它不能结构化控制流。

注意：

- `start/end` 是当前函数 instruction tuple 的 index，不是 byte offset。
- `locals_` 和 `stack` 是 core 传入的当前状态。
- 要把它们传给 `lift_steps()`。
- `result.stopped_at` 是 slice 内的 step 对象；返回 `VMLinearState.stopped_at` 时要换算成全局 instruction index。
- 不要自己合并 basic block。
- 不要根据特定 offset 写特殊逻辑。

## 17. make_step 完整模板

下面模板以 immediate target VM 为例。也就是说：跳转目标在 instruction operand 中，不在 operand stack 上。

如果你的 VM 是 stack target，仍然保留原始 decoded operands；只把常量传播恢复出的 target 用于 `VMHint.target`，不要用它替换原始 operands。

```python
from unidecompiler.core.vm_bytecode import VMBytecodeStep
from unidecompiler.core.vm_hints import VMHint
from unidecompiler.core.vm_operands import VMDecodedInstruction, VMOperand
from unidecompiler.core.ir import SourceRef


CONTROL = frozenset({"JUMP", "JUMP_IF_TRUE", "JUMP_IF_FALSE"})
CONDITIONAL = frozenset({"JUMP_IF_TRUE", "JUMP_IF_FALSE"})
STACK_MATERIALIZED_CONDITION = frozenset({"JUMP_IF_TRUE", "JUMP_IF_FALSE"})


def operand_for(instruction, index: int, value: object) -> VMOperand:
    # 真实 frontend 应按 opcode 语义逐个分类 operand。
    # 不能因为 opcode 是 control，就把所有 operand 都标成 target。
    if instruction.opcode in CONTROL and index == 0:
        return VMOperand(role="target", value=int(value), text=f"{int(value):#x}")
    if instruction.opcode == "LOAD_LOCAL":
        return VMOperand(role="local", value=int(value), text=f"local[{int(value)}]")
    if instruction.opcode == "CONST":
        return VMOperand(role="constant", value=int(value), text=f"const[{int(value)}]")
    return VMOperand(role="immediate", value=value, text=str(value))


def instruction_operands(instruction) -> tuple[VMOperand, ...]:
    return tuple(
        operand_for(instruction, index, value)
        for index, value in enumerate(instruction.operands)
    )


def control_target_for_hint(instruction, targets: dict[int, int] | None) -> int | None:
    if targets is None:
        return None
    return targets.get(instruction.offset)


def control_hints(instruction, source, targets: dict[int, int] | None) -> tuple[VMHint, ...]:
    if instruction.opcode not in CONTROL:
        return ()

    target = control_target_for_hint(instruction, targets)
    if target is None:
        return ()

    edge_hint = VMHint(
        kind="loop-backedge" if target <= instruction.offset else "branch-target",
        source=source,
        target=target,
        label=instruction.opcode,
        flow="unconditional" if instruction.opcode == "JUMP" else "conditional",
        detail="target-if-true" if instruction.opcode in CONDITIONAL else None,
    )

    if instruction.opcode in STACK_MATERIALIZED_CONDITION:
        return (
            edge_hint,
            VMHint(
                kind="materialized-condition",
                source=source,
                label=instruction.opcode,
                detail="stack",
                flow="conditional",
            ),
        )

    return (edge_hint,)


def make_step(program, instruction, targets: dict[int, int] | None = None) -> VMBytecodeStep:
    source = SourceRef(frontend=FRONTEND_ID, offset=instruction.offset, line=instruction.line)

    decoded = VMDecodedInstruction(
        opcode=instruction.opcode,
        source=source,
        operands=instruction_operands(instruction),
        raw=instruction.raw,
    )

    return VMBytecodeStep(
        opcode=instruction.opcode,
        source=source,
        decoded=decoded,
        raw=decoded.raw,
        effects=MY_EFFECT_TABLE.effects_for(program, instruction, source),
        hints=control_hints(instruction, source, targets),
    )
```

如果条件来自寄存器或 immediate，而不是前序 opcode 已经压到栈上的表达式，把该 opcode 从 `STACK_MATERIALIZED_CONDITION` 移除。

`branch_condition()` 接收到的 `branch` 是 `VMBytecodeStep`。如果需要读取 decoded operand，应通过 `branch.decoded.operands`，或在 frontend 私有 instruction 到 step 的映射中查回原始 instruction。不要假设 `VMBytecodeStep` 自带 `operands` 字段。

## 18. lift_function 完整模板

下面延续第 17 节的 immediate target 模板。若 target 需要常量传播，把 `immediate_absolute_targets(function)` 换成自己的 resolver，但 `targets` 的类型仍应是 `{branch_instruction_offset: target_offset}`。

```python
from unidecompiler.core.vm_function import VMFunctionSpec, lift_vm_step_function, recover_vm_function


def lift_function(function, program):
    targets = immediate_absolute_targets(function)
    steps = tuple(make_step(program, ins, targets) for ins in function.instructions)
    profile = build_hint_region_profile(
        steps,
        frontend=FRONTEND_ID,
        opcode_classes=REGION_CLASSES,
        raw_window=lambda index: raw_window(function.instructions, index),
    )
    spec = VMFunctionSpec(
        name=function.name,
        params=function.params,
        frontend=FRONTEND_ID,
        instruction_count=len(steps),
        local_names=function.local_names,
        metadata={"function_offset": function.offset},
    )

    return recover_vm_function(
        spec,
        lambda: lift_vm_step_function(
            spec,
            steps,
            profile=profile,
            stateful_callbacks=make_stateful_callbacks(program, function),
            raw_window=lambda index: raw_window(function.instructions, index),
        ),
        raw=tuple(ins.raw for ins in function.instructions),
    )
```

`recover_vm_function()` 会把意外异常转成可诊断的 unsupported。开发时仍应把 supported path 的 unsupported 修到零。

## 19. Module assembly

模块组装用 `assemble_vm_module()`：

```python
from unidecompiler.core.vm_module import assemble_vm_module


def lift_program(program, metadata):
    return assemble_vm_module(
        name=program.filename or f"<{FRONTEND_ID}-program>",
        source_language=FRONTEND_ID,
        metadata={"frontend": metadata, "bytecode_format": FRONTEND_ID},
        functions=tuple(lift_function(function, program) for function in program.functions),
    )
```

不要从 frontend 直接调用 `assemble_module()`、`assemble_function()` 或构造 `FunctionIR`。

## 20. GUI 展示与 bytecode_instructions

`lift_vm_step_function()` 会把 step 投影到 function metadata 的 `bytecode_instructions`，GUI 使用这些行展示反汇编和控制边。

每条展示行应有：

- `offset`
- `opcode`
- `operands`
- `raw`
- `source`
- `control`

如果 GUI 控制流视图崩溃，先检查：

- 是否有 target 不存在。
- 是否把 instruction index 当 byte offset。
- 是否出现 source/target 同 basic block 的自环展示边。
- 是否同一条条件跳转提交了多个互相矛盾的 target。

如果 core 内部 CFG 是正确的，但 GUI 展示元数据里有同块自环，可以只过滤展示 metadata 中的那条 control hint。不要改 core CFG，也不要丢掉真实恢复所需的 hint。

## 20.1 可选的模拟执行支持

模拟执行不是 frontend 的必选职责。一个 frontend 即使只能反编译、不能
模拟，也是合法的。只有当该 VM 的函数边界、调用约定和必要运行时事实
足够明确时，才应声明支持模拟。

模拟器与 frontend 的依赖方向必须保持严格解耦：

```text
frontend -> core generic IR <- unidecompiler-simulator <- CLI / GUI / host
```

这意味着：

- core 不能 import simulator，也不能知道 simulator 的类型或生命周期。
- simulator 只能执行 core 产出的 `ModuleIR`、`FunctionIR` 和其它公开 generic IR。
- simulator 不执行 frontend bytecode，不读取 decoder 私有模型，不解释 VM opcode，
  不直接执行 `Effect` 或 `VMBytecodeStep`。
- simulator 自己拥有 frame、调用、控制流、异常、步数限制、取消和 trace。
- frontend 不得因为支持模拟而新增一个语言解释器、opcode switch、VM stack
  executor 或专用控制流恢复器。
- CLI、GUI 和其它 host 只调用 simulator public API，不复制 frontend 的函数
  查找或执行逻辑。

### 20.1.1 什么时候应该支持模拟

建议在以下条件同时满足时支持：

1. decoder 能稳定识别函数边界，或能把平坦程序包装成稳定的入口函数。
2. `lift()` 能为目标函数生成语义正确的 generic IR。
3. 参数来源、返回值、调用约定和局部变量作用域足够明确。
4. 目标函数查询可以用稳定、可序列化的数据表示。
5. 语言特有的成员访问、闭包、间接调用或容器行为能够通过窄的运行时
   事实表达，而不需要 frontend 执行指令。
6. 可以为完成、异常、未支持操作和外部调用编写可重复的测试。

如果这些条件不满足，不要为了让 GUI 出现一个 Run 按钮而声明支持模拟。
保留反编译能力，并让 simulator 明确报告该 frontend 不支持 simulation。

### 20.1.2 simulation adapter 的职责

frontend 可以通过 plugin 的可选 `simulation_adapter` 属性提供 adapter：

```python
class MyVmFrontendPlugin:
    id = "my-vm"
    display_name = "My VM"
    supported_inputs = (".mvm",)
    simulation_adapter = MyVmSimulationAdapter
```

最小 adapter 形状如下：

```python
from unidecompiler_simulator import (
    NotHandled,
    ResolvedFunction,
    SimulationTargetCandidate,
)


class MyVmSimulationAdapter:
    frontend_id = "my-vm"

    def resolve_function(self, query, decoded_module, lifted_module):
        if not isinstance(query, str):
            return NotHandled

        matches = tuple(
            function
            for function in self._walk(lifted_module.functions)
            if function.name == query
        )
        if len(matches) != 1:
            # 0 个或多个匹配都不能猜测。
            return NotHandled
        return ResolvedFunction(matches[0], identifier=query)

    def list_simulation_targets(self, decoded_module, lifted_module):
        functions = tuple(self._walk(lifted_module.functions))
        counts = {}
        for function in functions:
            counts[function.name] = counts.get(function.name, 0) + 1
        return tuple(
            SimulationTargetCandidate(function.name, function.name)
            for function in functions
            if counts[function.name] == 1
        )

    @staticmethod
    def _walk(functions):
        for function in functions:
            yield function
            yield from MyVmSimulationAdapter._walk(function.nested_functions)
```

`resolve_function()` 的返回值必须是当前 lifted module 中的
`FunctionIR`。不能重新创建一个函数，不能返回 decoder 私有函数对象，不能
返回 Python callable。simulator 会再次验证函数归属关系。

`list_simulation_targets()` 的 query 是 frontend-owned opaque data。GUI 和
CLI 可以保存、显示和传递它，但不能解析它。query 必须是可安全传输的数据，
不能是函数、bound method、解释器对象、frame 或包含执行行为的对象。

如果函数名存在重载、匿名函数或多个闭包实例，frontend 必须选择一种稳定
且无歧义的 query，例如：

```text
Lua:     module.submodule.function
JVM:     Class.method(descriptor)
.NET:    Namespace.Type.Method(signature)
WASM:    export name 或 $funcN
Python:  唯一函数名或稳定的 nested-function 标识
```

不要在 simulator、GUI 或 CLI 中写这些语言的名称解析规则。

### 20.1.3 adapter 可以提供哪些运行时事实

adapter 可以实现 simulator 探测的窄操作，用于表达 generic IR 无法直接
表达、但又不需要执行 VM 的语言事实。常见操作包括：

| 操作 | 用途 |
|---|---|
| `resolve_global` | 将 frontend 语义中的全局名称解析为 `ResolvedFunction` 或 `IntrinsicCall` |
| `resolve_call` | 解析数据化的动态调用请求 |
| `resolve_indirect_call` | 解析受控的间接调用目标 |
| `truthy` | 提供语言定义的真假值规则 |
| `binary_op` | 提供语言特有的二元运算 |
| `unary_op` | 提供语言特有的一元运算 |
| `get_attr` / `set_attr` | 提供语言特有的成员访问 |
| `get_item` / `set_item` | 提供语言特有的索引或表访问 |
| `iterate` | 提供语言特有的可迭代值视图 |
| `set_captured` | 在闭包捕获变量赋值需要时提供数据化更新 |

这些 hook 必须满足：

- 只接收公开的 runtime value、字符串、数字和 data-only context。
- 返回 generic runtime value、`ResolvedFunction`、`IntrinsicCall` 或 `NotHandled`。
- 返回值必须通过 simulator 的 runtime-value 校验。
- `NotHandled` 表示不能安全处理，simulator 应产生显式 unsupported 或
  其它结构化失败，而不是猜测。
- hook 不得调用 frontend bytecode，不得递归执行 frontend interpreter。
- hook 不得返回 Python function、lambda、文件句柄、线程、模块、frame 或
  其它 executable callback。

`VMStatefulCallbacks` 和 `simulation_adapter` 是两个不同的边界：

```text
VMStatefulCallbacks: frontend -> core，用于 lifting 复杂 VM 线性片段和栈状态
simulation_adapter: frontend -> simulator，用于函数查询和运行时数据事实
```

simulator 不能调用 `VMStatefulCallbacks`，adapter 也不能借此把 VM 执行
逻辑转回 frontend。

### 20.1.4 外部函数和补环境

generic IR 中无法解析的命名函数，可以交给 host 提供的
`ExternalEnvironment`：

```python
from unidecompiler_simulator import (
    ExternalCallRequest,
    ExternalCallResult,
    ExternalCallStatus,
    NotHandled,
)


class MyEnvironment:
    def call(self, request: ExternalCallRequest):
        if request.name != "print":
            return NotHandled
        # 这里由 host 决定如何处理；返回值必须是受支持的 runtime value。
        return ExternalCallResult(
            ExternalCallStatus.RETURNED,
            values=(),
            stdout=" ".join(map(str, request.args)) + "\\n",
        )
```

环境协议是数据边界，不是执行控制边界。environment：

- 接收 `ExternalCallRequest`，不接收 IR、frame、stack、adapter 或 runner。
- 返回 `ExternalCallResult` 或 `NotHandled`。
- 只能返回 simulator 支持的 in-memory runtime values。
- 未处理的函数必须返回 `NotHandled`，不能伪造成功结果。
- 不应把 frontend 私有对象放入 request 或 result。

`runtime.py` 之类的文件属于 application host。它是用户明确选择的受信任
Python 代码，不是 sandbox。它应由独立的 host-support package 读取和加载，
不能由 core、simulator 或 frontend 加载。

注意：environment 只能补充目标函数执行过程中调用的外部函数，不能替代
`resolve_function()`，也不能提供 simulator 的目标函数本身。

### 20.1.5 SimulationResult 语义

frontend 不负责构造 `SimulationResult`，但测试和宿主必须正确处理这些结果：

| 状态 | 含义 | 要求 |
|---|---|---|
| `completed` | 函数正常返回 | 检查 `values`，不能只检查 status |
| `raised` | 执行产生语言/运行时异常 | 保留 exception 和 cause |
| `unsupported` | generic IR 或运行时事实无法安全表达 | 保留 diagnostic 和 trace context |
| `invalid_request` | query、参数或 environment 协议错误 | 明确显示错误 |
| `step_limit` | 达到最大执行步数 | 不能伪装为 completed |
| `call_depth_limit` | 达到最大调用深度 | 不能继续猜测 |
| `cancelled` | 用户或 host 请求取消 | 保留已产生的 trace |
| `yielded` | 执行遇到受支持范围外的 yield 行为 | 明确标注非 completed |

trace 限制只限制记录事件的数量，不得改变函数执行语义。被截断时必须
通过 `trace_truncated` 或等价诊断告知宿主。

### 20.1.6 模拟支持的最小交付流程

实现可选模拟时，按以下顺序执行：

1. 先完成 decoder、thin IR、generic IR 和反编译测试。
2. 确认函数边界、名称、参数和返回值已经稳定。
3. 实现 `simulation.py` 中的 `resolve_function()`。
4. 实现 `list_simulation_targets()`，过滤歧义目标。
5. 为一个纯计算函数写 `simulate_function()` 或 `simulate_artifact()` 测试。
6. 为分支、循环、容器/成员操作增加测试。
7. 如果需要语言特有行为，只增加窄的 data-only adapter hook。
8. 为一个未解析外部调用增加 `ExternalEnvironment` 测试。
9. 验证没有 environment 时得到显式 unsupported，而不是错误成功。
10. 通过 CLI 执行一次真实 artifact，确认参数和返回值保持不变。
11. 通过 GUI target discovery 和 Run 流程，确认 target 不被自动重置。
12. 检查 simulator 包没有被 core import，frontend 没有执行器或解释器。

先支持一个最小函数，再扩大覆盖范围。不要先为所有语言特性设计一个
frontend-specific runtime framework。

## 21. 什么时候期待 while，什么时候接受 goto

Frontend 的目标不是强行让输出出现 `while`。

正确预期：

- 简单单入口单出口循环：可能输出 `while`。
- 多出口循环、switch-like 分派、共享失败块、复杂 join：可能输出 `if/goto`。
- target 缺失或栈形状无法合并：应该 partial/unsupported。

如果复杂函数只输出线性代码，没有 `if`、没有 `goto`、也没有 unsupported，这是高风险信号。通常说明：

- 条件跳转 effect 提前消费了条件。
- 没有提交 `materialized-condition`。
- branch target 没恢复。
- `branch_stack_width` 错。
- 条件极性 hint 错。
- profile 没把 opcode 标成 control/jump/conditional。

验证时要明确区分：

```text
结构化好结果：has_while 或 has_if
保守正确结果：has_if + has_goto
危险结果：复杂 CFG 被线性化
```

## 22. 错误、unsupported 与诊断

Decoder 错误：

- 输入不是该格式：`can_load=False`。
- 格式匹配但损坏：`decode` 抛 `FrontendDecodeError`。
- 未知版本：metadata 或 diagnostics 中明确报告。

Lift 错误：

1. 仍然提交指令和 raw 文本。
2. 使用 `UnknownOpcode` 或 `effects=None`。
3. 提供 `raw_window`、decoded operands、target/region hints。
4. 让 core 返回 `partial` 或 `unsupported`，不要猜测。

`unsupported` 不是开发终点。支持范围内出现 unsupported，应该通过：

- 修正 opcode effect。
- 补 target/case/exception hints。
- 补 stateful callbacks。
- 补 VM-neutral thin IR concept。
- 或在 core 中增强 VM-neutral recovery。

不要把 unsupported 用 frontend 特例“绕过去”。

## 23. 验证脚本

外部目录插件 smoke test：

```python
from pathlib import Path
from unidecompiler import DecompilerEngine
from unidecompiler.plugin_registry import FrontendRegistry

plugin_dir = "/path/to/my-vm-plugin"
sample = "/path/to/sample.mvm"

registry = FrontendRegistry.discover()
registry.register_directory(plugin_dir)

result = DecompilerEngine.from_registry(registry).decompile_bytes(
    Path(sample).read_bytes(),
    sample,
    "my-vm",
)

text = result.pseudocode.text if result.pseudocode is not None else ""
print("status", result.status, result.frontend_id)
print("functions", [(f.name, f.status, f.unsupported_reason) for f in result.functions])
print("diagnostics", [(d.code, d.severity) for d in result.diagnostics])
print("has_if", "if (" in text or "if " in text)
print("has_goto", "goto block_" in text)
print("has_while", "while" in text)
print("has_unsupported", "unsupported" in text)
print("cfg", [(len(g.blocks), len(g.edges)) for g in result.control_flow])
print("self_edges", [e for g in result.control_flow for e in g.edges if e.source == e.target])
```

严格成功样本断言：

```python
assert result.pseudocode is not None
assert result.status == "ok"
assert result.frontend_id == "my-vm"
assert result.functions
assert "unsupported" not in text
assert result.control_flow
assert any(len(g.blocks) >= 1 for g in result.control_flow)
assert not [e for g in result.control_flow for e in g.edges if e.source == e.target]
```

partial 可接受样本断言：

```python
assert result.pseudocode is not None
assert result.status in {"ok", "partial"}
assert result.frontend_id == "my-vm"
assert result.functions
assert result.control_flow
assert any(len(g.blocks) >= 1 for g in result.control_flow)
assert not [e for g in result.control_flow for e in g.edges if e.source == e.target]
```

不要把两类断言混用。支持范围内的核心路径应使用严格断言；正在扩展语义覆盖时，可以临时使用 partial 可接受断言，但要把 unsupported 原因纳入修复列表。

如果当前样本本应完全支持：

```python
assert result.status == "ok"
assert result.frontend_id == "my-vm"
assert result.functions
assert "unsupported" not in text
```

对复杂控制流样本，至少应满足：

```text
status ok
frontend_id 是你的 frontend id
没有 unsupported 文本
CFG 有多个 blocks/edges
伪代码有 while/if，或者至少有 if/goto
GUI control_flow 无自环崩溃
```

如果目标是“语义完整”，`partial` + `if/goto` 可以接受。如果目标是“高级结构化”，需要 core 能安全匹配该 CFG 形状。

复杂控制流断言可以更严格：

```python
has_structured_control = "while" in text or "if (" in text or "if " in text
has_low_level_cfg = "goto block_" in text
has_cfg_shape = any(len(g.blocks) > 1 and len(g.edges) > 0 for g in result.control_flow)

assert has_cfg_shape
assert has_structured_control or has_low_level_cfg
```

如果样本已知有条件跳转和后向边，可以直接检查展示 CFG：

```python
edges = [edge for graph in result.control_flow for edge in graph.edges]
assert any(edge.kind == "branch" for edge in edges)
assert any(int(edge.target.split("_")[-1]) <= int(edge.source.split("_")[-1]) for edge in edges)
```

这些断言不是要求所有 VM 都输出 `while`。它们要求复杂控制流不能被误线性化。

### 23.1 模拟执行验证脚本

如果 frontend 声明支持模拟，必须在反编译 smoke test 之外增加 simulator
验证。以下示例使用 public simulator API，不直接调用 frontend 的 decoder
私有函数或执行器：

```python
from pathlib import Path

from unidecompiler.input_sources import InputEntry, load_input_entry
from unidecompiler.plugin_registry import FrontendRegistry
from unidecompiler_simulator import SimulationEngine, SimulationStatus


sample = Path("/path/to/sample.mvm")
registry = FrontendRegistry.discover()
registry.register_directory("/path/to/my-vm-plugin")
simulator = SimulationEngine.from_registry(registry)

artifact = load_input_entry(InputEntry(sample, str(sample)))
listing = simulator.list_artifact_targets(artifact.data, artifact.display_path)
assert listing.frontend_id == "my-vm"
assert listing.diagnostic is None
assert listing.targets

target = next(target for target in listing.targets if target.label == "add")
result = simulator.simulate_artifact(
    artifact.data,
    artifact.display_path,
    target.query,
    args=(2, 3),
)

assert result.status is SimulationStatus.COMPLETED
assert result.values == (5,)
assert result.exception is None
assert result.diagnostic is None
assert result.steps > 0
```

目标发现测试必须覆盖：

```python
assert listing.targets
assert all(target.query is not None for target in listing.targets)
assert all(target.function_index >= 0 for target in listing.targets)
assert len({target.label for target in listing.targets}) == len(listing.targets)
```

歧义查询不能猜测。可以不列出歧义 target，也可以让执行返回明确的
`invalid_request`，但不能随机选择函数：

```python
ambiguous = simulator.simulate_artifact(
    artifact.data,
    artifact.display_path,
    "overloaded",
    args=(),
)
assert ambiguous.status is SimulationStatus.INVALID_REQUEST
assert ambiguous.diagnostic
```

外部 environment 测试必须验证返回值、stdout、异常和未处理调用：

```python
from unidecompiler_simulator import (
    ExternalCallResult,
    ExternalCallStatus,
    NotHandled,
)


class TestEnvironment:
    def call(self, request):
        if request.name == "print":
            return ExternalCallResult(
                ExternalCallStatus.RETURNED,
                values=(),
                stdout=repr(request.args) + "\\n",
            )
        return NotHandled


completed = simulator.simulate_artifact(
    artifact.data,
    artifact.display_path,
    "prints_value",
    args=(7,),
    environment=TestEnvironment(),
)
assert completed.status is SimulationStatus.COMPLETED
assert any(event.kind == "external-call" for event in completed.events)
assert any("7" in event.stdout for event in completed.events)

without_environment = simulator.simulate_artifact(
    artifact.data,
    artifact.display_path,
    "prints_value",
    args=(7,),
)
assert without_environment.status is SimulationStatus.UNSUPPORTED
assert without_environment.diagnostic
```

限额、取消和 trace 截断也属于 frontend integration 的验证范围：

```python
from unidecompiler_simulator import SimulationCancellation, SimulationLimits


limited = simulator.simulate_artifact(
    artifact.data,
    artifact.display_path,
    "loop",
    limits=SimulationLimits(max_steps=20, max_trace_events=5),
)
assert limited.status is SimulationStatus.STEP_LIMIT
assert limited.trace_truncated or len(limited.events) <= 5

cancellation = SimulationCancellation()
cancellation.cancel()
cancelled = simulator.simulate_artifact(
    artifact.data,
    artifact.display_path,
    "loop",
    cancellation=cancellation,
)
assert cancelled.status is SimulationStatus.CANCELLED
```

这些测试必须验证 generic IR 执行结果，而不是验证 frontend 自己重新执行
字节码后得到的结果。推荐把源代码、生成 artifact 和期望值放到
`simulator_projects/source/<project>` 及对应的期望文件中。

## 24. 单元测试清单

Decoder：

- 正确识别 header/magic/version/endianness。
- 正确处理扩展名。
- 截断输入报错。
- 错误长度报错。
- 损坏常量表报错。
- 所有 opcode 都有 offset、size、operands、raw。
- 空输入报错。
- 非本格式输入 `can_load=False`。

Effect：

- 常量 push。
- local load/store。
- 二元运算顺序。
- 调用参数顺序。
- return/halt。
- unknown opcode。

Control：

- unconditional jump target。
- conditional jump target。
- backward jump -> `loop-backedge`。
- materialized condition。
- branch polarity。
- invalid target。
- switch/case/default。
- exception region。

Integration：

- 线性函数输出伪代码。
- if/else。
- while 或低层 if/goto。
- nested control。
- 多函数模块。
- 一个函数失败不影响其他函数。
- GUI 可以打开 sample，不显示为 resource。

如果 frontend 声明支持 simulation，还必须增加：

Target discovery：

- `simulation_adapter` 可以被 registry 识别。
- `list_simulation_targets()` 返回稳定、唯一、data-only 的 query。
- 不支持或歧义的函数不会被随机列出。
- 每个列出的 query 都能解析到当前 `ModuleIR` 中的 `FunctionIR`。
- nested function、匿名函数和重载函数的命名策略有测试。

Generic-IR execution：

- `simulate_function()` 可以执行一个没有 frontend 私有 runtime 依赖的纯函数。
- `simulate_artifact()` 可以通过 frontend query 找到同一个函数。
- 参数绑定顺序、默认参数行为和返回值数量正确。
- 分支、循环、比较、容器、成员和闭包场景覆盖目标 VM 的支持范围。
- 一个函数的 simulation failure 不会破坏其它函数的 decompile result。

Adapter boundary：

- adapter 只返回 `ResolvedFunction`、`IntrinsicCall`、runtime value 或 `NotHandled`。
- adapter 不包含 execute/run/step/eval/interpreter 逻辑。
- adapter 不返回 executable callback、frame、stack 或 frontend 私有执行对象。
- adapter 返回的函数属于当前 lifted module。
- 未处理的 global、call、attribute、item 或 iterator 行为产生显式结果。

Environment and outcomes：

- `ExternalEnvironment` 能处理至少一个外部调用。
- 没有 environment 时，未解析调用得到显式 `unsupported`，不能假装成功。
- host 返回值、stdout、stderr 和异常都经过结构化结果传递。
- `completed`、`raised`、`unsupported`、`invalid_request`、step limit、call
  depth limit 和 cancellation 至少覆盖目标支持范围内的相关状态。
- trace 截断不会改变返回值或控制流结果。

Host integration：

- CLI 只传递 frontend-owned query 并显示 `SimulationResult`。
- GUI 自动列举 target，不实现 frontend-specific lookup。
- GUI Run 后目标选择不被重置，返回值、状态和 trace 都可见。
- runtime 文件加载发生在 application host，frontend 和 simulator 不加载文件。

## 25. 命令行和 GUI 注册

Python API 注册：

```python
from unidecompiler import DecompilerEngine
from unidecompiler.plugin_registry import FrontendRegistry

registry = FrontendRegistry.discover()
registry.register_directory("/path/to/my-vm-plugin")
engine = DecompilerEngine.from_registry(registry)
```

GUI 注册：

```text
Frontend manager -> Register folder -> /path/to/my-vm-plugin
```

如果 GUI 仍显示为 resource：

- 检查 `can_load()` 是否对扩展名返回 true。
- 检查 manifest 路径是否注册的是插件根目录。
- 检查 `module` 能否 import。
- 检查 plugin id 是否和 decompile 选择一致。
- 检查 GUI 当前 registry 是否需要重新注册或重启。

如果 GUI 可以反编译但 Simulation tab 没有 target：

- 确认 plugin 暴露了 `simulation_adapter`，且 adapter 的 `frontend_id` 与
  plugin id 完全一致。
- 确认 adapter 实现了 `resolve_function()`。
- 确认 `list_simulation_targets()` 返回的是
  `SimulationTargetCandidate` 元组，而不是 `FunctionIR` 或 callable。
- 确认每个 candidate 的 query 能被 `resolve_function()` 唯一解析。
- 确认列出的函数已经被 `lift()` 放入当前 `ModuleIR`，包括 nested functions。
- 确认 GUI 使用的是包含该 plugin 的同一个 registry。

CLI/GUI 不应通过修改扩展名判断是否支持模拟。输入识别仍由动态注册的
frontend registry 决定，simulation target 发现也由对应 adapter 决定。

## 26. 常见错误表

| 错误 | 结果 | 正确做法 |
|---|---|---|
| frontend 构造 `If`/`While`/AST | 破坏 core ownership | 只提交 effects/hints |
| 只提交简单 opcode | 复杂样本丢上下文 | 所有可解码 opcode 都提交 |
| unknown opcode 返回空 tuple | 误导性伪代码 | 用 `UnknownOpcode` 或 unsupported |
| branch target 用 instruction index | CFG 错位 | 用原始 bytecode offset |
| 条件跳转 effect 提前 `Pop` 条件 | 没有 `if/goto` | 用 `branch_stack_width` 消费 |
| 没有 `materialized-condition` | 条件可能被线性化 | 条件栈 VM 提交该 hint |
| 条件极性没标 | true/false 边反 | `detail="target-if-true"` |
| 后向边仍只标 branch-target | loop 信息弱 | 后向边用 `loop-backedge` |
| 把私有对象放进 operand | core/frontend 耦合 | 只用中立 value/text |
| metadata 表达程序逻辑 | 恢复不可测试 | 用 `VMHint` |
| backend 推断 loop/goto | 多 frontend 不一致 | core structuring 负责 |
| GUI 自环边崩溃 | CFG 视图不可用 | 修正/过滤展示 metadata |
| 用 subprocess 解析 | 平台和诊断不稳定 | 用库或本地 parser |
| frontend 为模拟执行字节码 | simulator/frontend 双重语义 | frontend 只提供 adapter，simulator 执行 generic IR |
| adapter 按名称随意选重载 | 运行了错误函数 | 使用稳定 query，歧义时 `NotHandled` |
| adapter 返回 callable 或 decoder object | 执行边界泄漏 | 只返回 data-only value、`ResolvedFunction` 或 `NotHandled` |
| GUI/CLI 自己解析类名或 Lua 名称 | host 与 frontend 语义分叉 | 只传 opaque query 给 adapter |
| runtime.py 放进 simulator | core/host 耦合且无法审计 | 由独立 host-support package 加载受信任文件 |
| 未处理外部调用返回空值 | 伪造成功结果 | 返回 `NotHandled`，让 simulator 结构化失败 |
| trace 截断后停止或改变结果 | 观察行为改变执行语义 | 只截断事件，继续受限执行 |

## 27. 从零实现顺序

推荐顺序：

1. 建目录和 manifest。
2. 写 `model.py`。
3. 写 `decoder.py`，先通过 `can_load/decode`。
4. 写最小 `plugin.py`。
5. 写 `lifter.py`，先覆盖线性 opcode。
6. 写小样例测试二元运算和调用参数顺序。
7. 加 branch target resolver。
8. 加 `VMRegionOpcodeClasses`。
9. 加 `VMStatefulCallbacks`。
10. 加 `branch_condition` 和 `branch_stack_width`。
11. 给条件跳转加 `target-if-true` 和 `materialized-condition`。
12. 给后向边加 `loop-backedge`。
13. 跑复杂控制流样本，确认至少有 `if/goto`。
14. 补 unknown/malformed 测试。
15. 如果支持模拟，实现 `simulation.py` 的 target discovery 和函数解析。
16. 为纯计算函数、控制流、返回值和参数绑定增加 simulator project。
17. 为语言特有行为增加最小 data-only adapter hook，不要增加解释器。
18. 为外部调用增加 `ExternalEnvironment` 测试，并测试未处理结果。
19. 通过 CLI 执行真实 artifact，确认 query、args、return values 正确。
20. 注册 GUI，确认不是 resource、target 能发现、Run 后 trace 可见。
21. 检查 adapter 和 host 没有执行 frontend bytecode 的路径。
22. 再看是否需要 core 增强高级结构化。

## 28. 完成标准

一个 frontend 在目标支持范围内完成，至少要满足：

- decoder 能稳定解析目标文件。
- 每条可解码 instruction 都生成 step。
- `SourceRef` offset 正确。
- effect table 覆盖所有已知 opcode。
- unknown opcode 可诊断。
- 控制流 target 正确。
- 条件跳转不被错误线性化。
- 复杂控制流至少输出低层 `if/goto`。
- 没有误导性的成功状态。
- GUI 能注册、识别、反编译、显示 CFG。
- 测试覆盖 decoder、effects、control hints、integration。

模拟支持是可选的，不支持模拟不会使 frontend 反编译能力不合格。如果
frontend 声明支持模拟，还必须满足：

- `simulation_adapter` 只提供 data-only target lookup 和 runtime facts。
- 所有 simulation target 都能解析到当前 lifted `ModuleIR` 的函数。
- simulator 执行 generic IR，frontend 不执行字节码、不维护模拟器 frame/stack。
- 测试覆盖 target discovery、歧义查询、参数、返回值和目标语言的运行时事实。
- 测试覆盖至少一个控制流场景和一个外部 environment 场景。
- 无法解析的调用、unsupported IR、异常、超限和取消都有结构化结果。
- CLI/GUI 只消费 public simulator API，不实现语言专有查找和执行逻辑。
- `simulator_projects` 中有源代码、生成 artifact 和可重复的期望值验证。

如果这些都满足，但仍不能输出高级 `while/for/switch`，这通常不是 frontend 缺陷，而是 core 当前结构化能力边界。Frontend 不能为了显示更漂亮而绕过 core。

## 29. 选择你的 VM 建模路径

不同 VM 的实现入口不同，但最终都要提交同一种 thin IR。

先判断 VM 属于哪类，再选建模策略。

| VM 类型 | 常见特征 | frontend 建模方式 | 控制流重点 |
|---|---|---|---|
| 纯栈机 | opcode 从 operand stack 取值 | `Push`、`Pop`、`Binary`、`CallStackArgs` | 栈上条件、栈上 target、`branch_stack_width` |
| 寄存器 VM | opcode 显式读写寄存器/槽位 | 用 `LoadLocal`/`StoreLocal` 或 register 命名 locals | branch condition 多来自寄存器表达式 |
| 累加器 VM | 隐式 accumulator | 把 accumulator 映射为稳定 local，例如 `acc` | 每条运算要更新 `acc` |
| 三地址码 VM | `dst = op src1 src2` | `LoadLocal` + `Binary` + `StoreLocal`，或直接 `AssignValue` | target 通常是 immediate/relative |
| typed stack VM | 栈值有类型 | effect 保持表达式，metadata 可保留类型 | merge 点类型一致性很重要 |
| native-like bytecode | 有地址、跳表、间接跳转 | 保守 target recovery，未知间接跳转 partial | 不要猜 computed jump |
| AST-ish bytecode | opcode 已接近语法节点 | 仍提交 thin facts，不构造 AST | 让 core 统一恢复结构 |

如果 VM 不是栈机，不要强行套用栈机例子。目标是表达等价事实，而不是模拟文档里的 opcode 名。

## 30. API 契约速查

本节把常用对象的字段集中列出，便于写代码时对照。

### FrontendPlugin

| 成员 | 类型 | 必需 | 说明 |
|---|---|---:|---|
| `id` | `str` | 是 | 稳定 frontend id |
| `display_name` | `str` | 是 | GUI 展示名 |
| `supported_inputs` | `tuple[str, ...]` | 推荐 | 展示和选择辅助 |
| `version_support` | `FrontendVersionSupport` | 推荐 | 支持版本说明 |
| `can_load(data, filename)` | method | 是 | 快速判断输入是否可能属于该 frontend |
| `decode(data, filename)` | method | 是 | 返回 `FrontendModule` 或抛 `FrontendDecodeError` |
| `lift(module)` | method | 是 | 返回 `ModuleIR` |

`can_load()` 不应抛普通解析错误。遇到“可能是本格式但内容损坏”的情况，可以返回 `True`，再由 `decode()` 给出精确错误。

### FrontendModule

| 字段 | 类型 | 说明 |
|---|---|---|
| `frontend_id` | `str` | 必须等于 plugin id |
| `payload` | `object` | frontend 私有 decoder 模型 |
| `metadata` | `dict` | provenance、版本、diagnostics、统计信息 |

`payload` 不会被 core 解释。只有同一个 frontend 的 `lift()` 可以读取它。

### VMBytecodeStep

| 字段 | 类型 | 必需 | 说明 |
|---|---|---:|---|
| `opcode` | `str` | 是 | 稳定 opcode 名 |
| `source` | `SourceRef` | 是 | 原始来源 |
| `decoded` | `VMDecodedInstruction | None` | 推荐 | GUI/CLI 展示 |
| `raw` | `str` | 推荐 | 原始反汇编文本 |
| `effects` | `tuple[Effect, ...] | None` | 是 | thin stack/value facts |
| `hints` | `tuple[VMHint, ...]` | 推荐 | 控制流/调用/聚合等事实 |

`effects` 的三种状态：

| 写法 | 含义 | 典型用途 |
|---|---|---|
| `()` | 明确无语义 | `NOP`、padding |
| `(UnknownOpcode(...),)` | 已知指令边界但语义未知 | 保留 raw context |
| `None` | 该 opcode 当前无法安全表达 | 让 core partial/unsupported |

不要用 `()` 表示“还没实现”。

### VMHint

| 字段 | 类型 | 说明 |
|---|---|---|
| `kind` | `str` | hint 类型 |
| `source` | `SourceRef` | hint 来源 |
| `target` | `int | None` | bytecode target offset |
| `value` | `object | None` | case value、region dict、shape 信息 |
| `label` | `str` | 展示标签 |
| `detail` | `str | None` | 中立细节，例如 `target-if-true` |
| `flow` | `str | None` | `conditional`、`unconditional`、`multiway` |

常用字段组合：

| kind | 必填 | 可选 | 说明 |
|---|---|---|---|
| `branch-target` | `target` | `flow`、`detail`、`label` | 前向或普通跳转 |
| `loop-backedge` | `target` | `flow`、`detail`、`label` | 后向边 |
| `case-target` | `target`, `value` | `label` | switch case |
| `default-target` | `target` | `label` | switch default |
| `fallthrough` | `target` | `label` | 显式 fallthrough fact |
| `materialized-condition` | 无 | `detail`, `flow` | 条件已在栈/寄存器中物化 |
| `exception-region` | `value` | `label` | try/protected range |
| `call-shape` | `value` | `label` | 调用参数/返回形状 |
| `aggregate-shape` | `value` | `label` | array/map/object shape |

### VMStatefulCallbacks

| callback | 输入 | 返回 | 说明 |
|---|---|---|---|
| `initial_locals` | 无 | `dict[str, Expr]` | 函数入口 locals |
| `lift_linear` | `start, end, locals, stack` | `VMLinearState | None` | 解释线性 slice |
| `branch_condition` | `branch, stack_slice` | `Expr | None` | 从消费栈片段构造条件 |
| `branch_stack_width` | `instruction` | `int` | branch 在 CFG 语义上消费的栈值数量 |

`branch_stack_width` 不是“opcode operand 数”。它是 core 在分支边上应从 stack state 移除的值数。

如果 branch target 是 immediate，不在栈上，width 通常只包含 condition。如果 condition 和 target 都在栈上，width 通常是 2。

### SimulationAdapter

`SimulationAdapter` 是可选的 frontend 能力，不属于 core lifting API。

| 成员 | 输入 | 返回 | 说明 |
|---|---|---|---|
| `frontend_id` | 无 | `str` | 必须等于 plugin id |
| `resolve_function` | `query`, `decoded_module`, `lifted_module` | `ResolvedFunction` 或 `NotHandled` | frontend-specific 目标解析 |
| `list_simulation_targets` | `decoded_module`, `lifted_module` | `tuple[SimulationTargetCandidate, ...]` 或 `NotHandled` | GUI/CLI 的 target discovery |

可选运行时 facts 使用同一个 data-only adapter，但不是执行入口：

| hook | 作用 |
|---|---|
| `resolve_global` | 解析全局名称为 `ResolvedFunction` 或 `IntrinsicCall` |
| `resolve_call` | 解析动态调用请求 |
| `resolve_indirect_call` | 解析受控的间接调用 |
| `truthy` | 语言特有真假值 |
| `binary_op` / `unary_op` | 语言特有运算 |
| `get_attr` / `set_attr` | 成员访问 |
| `get_item` / `set_item` | 索引访问 |
| `iterate` | 迭代器值视图 |
| `set_captured` | 捕获变量更新 |

以上 hook 缺省都应返回 `NotHandled`。返回值必须是 validated generic runtime
value、`ResolvedFunction`、`IntrinsicCall` 或 `NotHandled`。不能返回 callable、
frame、stack、module、decoder model 或其它执行对象。

### ExternalEnvironment

`ExternalEnvironment` 由 CLI、GUI 或 embedding host 注入，不由 frontend
自动创建：

| 类型 | 作用 |
|---|---|
| `ExternalCallRequest` | 函数名、参数、关键字参数、caller 和 source 的数据请求 |
| `ExternalCallResult` | returned、raised 或 not handled 的结构化结果 |
| `NotHandled` | host 不负责该调用 |

environment 不接收 `ModuleIR`、`FunctionIR`、frame、stack、adapter 或 runner。
`runtime.py` 的加载属于 host-support package，且是受信任代码执行，不是
simulator sandbox。

## 31. Effect cookbook

下面是常见 effect 的选择准则。

| 目标语义 | 推荐 effect | 栈输入 | 栈输出 | 备注 |
|---|---|---|---|---|
| 压常量 | `Push(Const(...))` | 0 | 1 | 常量值必须是稳定值 |
| 弹弃值 | `Pop(count=n)` | n | 0 | 不要用于条件跳转提前消费 |
| 复制栈值 | `Copy`/`DuplicateTop` | 视 effect | 视 effect | 用于 dup 类 opcode |
| 交换顺序 | `Swap(depth=n)` | n | n | 常见于非默认操作数顺序 |
| 二元运算 | `Binary(op=...)` | 2 | 1 | 先确认 left/right 顺序 |
| 比较 | `Binary(op=\"==\")` 或 `Compare` | 2 | 1 | 输出条件表达式 |
| 读 local | `LoadLocal(name=...)` | 0 | 1 | 寄存器可映射为 local |
| 写 local | `StoreLocal(name=...)` | 1 | 0 | 用稳定 local 名 |
| 调用 | `CallStackArgs`/`Invoke` | argc | returns | runtime API 用可读 callee |
| 返回栈顶 | `ReturnTop` | 1 | 终止 | 函数返回值 |
| 无值返回 | `ReturnVoid` | 0 | 终止 | halt/void return |
| 未知 | `UnknownOpcode` | 未定 | unsupported | 保留 raw |

选择 effect 时优先保持语义精确。无法精确表达时，不要写“看起来能跑”的近似 effect。

## 32. 寄存器 VM 建模模板

寄存器 VM 不需要把所有东西伪装成 operand stack。

可以把寄存器映射为 core local：

```python
def reg_name(index: int) -> str:
    return f"r{index}"
```

三地址运算：

```text
ADD dst, left, right
```

可建模为：

```python
return (
    LoadLocal(source=src, name=reg_name(left)),
    LoadLocal(source=src, name=reg_name(right)),
    Binary(source=src, op="+", semantics="static"),
    StoreLocal(source=src, name=reg_name(dst)),
)
```

寄存器条件跳转：

```text
JUMP_IF_ZERO r3, target
```

如果 target 是 immediate，effect 不需要保留 target 在栈上。

有两种安全写法。

第一种：在该 branch instruction 的 effect 中把寄存器条件临时压到 core stack，然后让 `branch_stack_width` 消费 1 个条件值。这适合当前 core 的 stateful branch callback 模型：

```python
def effects_for_jump_if_zero(ins, src):
    return (LoadLocal(source=src, name=reg_name(ins.condition_reg)),)


def branch_condition(branch, stack):
    condition = stack[-1]
    return BinaryOp(source=condition.source, op="==", left=condition, right=Const(value=0, source=condition.source))


branch_stack_width=lambda ins: 1 if ins.opcode == "JUMP_IF_ZERO" else 0
```

这种写法不是“提前 pop 条件”。它只是把寄存器条件 materialize 成 branch callback 可见的表达式；真正从 stack state 移除的是 `branch_stack_width`。

同时提交 target hint：

```python
VMHint(
    kind="branch-target",
    source=src,
    target=ins.target,
    flow="conditional",
    detail="target-if-true",
)
```

这类 VM 通常不需要 `materialized-condition`，因为条件不是前序 opcode 已经留在 operand stack 上的值，而是 branch instruction 自己读取寄存器。

第二种：如果后续 core 扩展支持直接从 decoded operand 生成 branch condition，可以让 callback 读取 `branch.decoded.operands` 中的 register operand 并构造 `LoadLocal`/`BinaryOp`。在当前文档模板里优先使用第一种，因为它只依赖已有 effects/callbacks。

## 33. Binary decoder cookbook

二进制 VM decoder 要先把容器和 instruction stream 分清楚。

建议顺序：

1. 检查 magic/header。
2. 读取 version。
3. 确定 endianness。
4. 解析 section table 或 code offset。
5. 解析 constant pool。
6. 解析函数表或入口点。
7. 解析 instruction stream。
8. 解析 debug/local/symbol 信息。
9. 校验 branch target 是否落在指令边界。
10. 保存 raw bytes 或反汇编文本。

固定宽度 instruction：

```python
offset = code_start
while offset < code_end:
    opcode = data[offset]
    operand = int.from_bytes(data[offset + 1:offset + 4], byteorder)
    instructions.append(MyInstruction(offset=offset, opcode=opcode_name(opcode), size=4, operands=(operand,)))
    offset += 4
```

变长 instruction：

```python
offset = code_start
while offset < code_end:
    opcode = data[offset]
    size, operands = decode_operands(data, offset, opcode)
    if size <= 0 or offset + size > code_end:
        raise FrontendDecodeError(f"truncated instruction at {offset:#x}")
    instructions.append(MyInstruction(offset=offset, opcode=opcode_name(opcode), size=size, operands=operands))
    offset += size
```

Relative branch target：

```python
target = instruction.offset + instruction.size + signed_delta
```

Absolute branch target：

```python
target = code_base + absolute_offset
```

无论哪种 target，最后提交给 `VMHint.target` 的值都必须和 `SourceRef.offset` 使用同一坐标系。

## 34. Target recovery 策略

Target 来源常见有六种。

| 来源 | 策略 | 失败时 |
|---|---|---|
| immediate absolute | 直接解析 | invalid target diagnostic |
| immediate relative | `offset + size + delta` | invalid target diagnostic |
| constant pool label | 查表解析 | unknown label unsupported |
| stack constant | 局部常量传播 | 算不出就不猜 |
| register constant | 数据流/fixpoint | 算不出就 partial |
| jump table | `case-target/default-target` | 缺项就 partial |

简单线性常量传播只能覆盖无 join 或 join 不影响 target 的情况。

遇到分支 join、循环或寄存器 target 时，应使用保守数据流：

```text
每个 program point 保存 abstract state
常量值为 int
未知值为 Unknown
不同常量 merge 后变 Unknown
worklist 直到 fixpoint
只提交确定为单一 int 且在 valid_offsets 中的 target
```

不要提交“最可能”的 target。错误 target 比 unknown target 更糟，因为它会产生误导性 CFG。

## 35. Switch 和 jump table

Switch-like opcode 不应伪装成一串普通条件跳转。

如果 VM 明确提供多路分发：

```text
SWITCH selector, default, [(case0, target0), (case1, target1)]
```

提交：

```python
hints = (
    VMHint(kind="default-target", source=src, target=default_target, flow="multiway"),
    VMHint(kind="case-target", source=src, value=case0, target=target0, flow="multiway"),
    VMHint(kind="case-target", source=src, value=case1, target=target1, flow="multiway"),
)
```

`branch_stack_width` 应覆盖 selector 和任何栈上 target。如果 selector 来自寄存器或 immediate，不要把它算进栈消费。

Jump table 常见错误：

- 忘记 default target。
- case value 和 case index 混用。
- table entry 是 relative offset，但按 absolute 提交。
- 把无法解析的 computed jump 猜成 switch。

## 36. Exception region

异常表是控制流事实，应该用 hint 表达。

典型信息：

```text
protected_start
protected_end
handler_target
exception_type
stack_depth
binding_name
```

可提交：

```python
VMHint(
    kind="exception-region",
    source=src,
    value={
        "start": protected_start,
        "end": protected_end,
        "target": handler_target,
        "type": exception_type,
    },
)
```

规则：

- `start/end/target` 使用同一 offset 坐标系。
- `end` 是 VM 格式定义的边界，通常是半开区间末尾。
- handler target 必须落在 instruction boundary。
- exception type 可以是中立字符串或常量标识。
- 不要把 try/catch AST 节点放进 frontend。

如果 VM 有 finally、filter、fault、resume 等复杂语义，而现有 hint 不能表达，应优先补 VM-neutral fact 或让 core partial。

## 37. 函数发现与调用约定

很多 frontend 的难点在函数发现，不在单条 opcode。

函数来源可能是：

- 显式函数表。
- debug/symbol table。
- 入口点 + call target 递归发现。
- section metadata。
- 固定 offset。
- 平坦程序包装成 `main`。

规则：

- 每个函数独立生成 `VMFunctionSpec`。
- 一个函数 unsupported 不应阻止其他函数。
- 函数名没有时用稳定合成名，例如 `sub_0040`。
- `params` 只放确定的参数。
- `local_names` 只放 VM/debug 信息确认的 locals。
- call target 不确定时，调用可以保守表达为 indirect call。

调用约定需要明确：

| 问题 | 示例 |
|---|---|
| 参数来自哪里 | stack、register、locals、argument area |
| 返回值放哪里 | stack、register、memory |
| call 是否清栈 | caller-clean、callee-clean |
| 是否可能抛异常 | exception edge |
| 是否有 closure/upvalue | captured locals |

如果调用约定不清楚，不要为了美观生成错误参数列表。优先保留低层调用形态。

### 37.1 模拟目标发现和查询约定

函数发现同样决定 simulation 是否可用。反编译阶段可以使用合成名称或保守
的 indirect call；模拟阶段则必须能把用户选择的 target query 唯一映射到
当前 lifted module 中的一个函数。

为每种 frontend 在 README 和测试中记录：

| 项目 | 必须明确的问题 |
|---|---|
| target label | GUI/CLI 显示给用户的稳定名称是什么 |
| query | frontend 接收的 data-only 标识是什么 |
| 唯一性 | 重载、同名 nested function、匿名函数如何消歧 |
| 参数 | 参数名是否可靠，是否支持 keyword 参数 |
| receiver/context | instance method、closure、upvalue 如何以 data-only context 表达 |
| 外部调用 | 哪些调用由 adapter 解析，哪些交给 environment |

推荐策略：

- 名称全局唯一时，query 可以是字符串名称。
- 同名方法存在时，query 应包括 owner 和 descriptor/signature。
- 匿名函数应使用稳定的 source offset、function index 或 frontend-defined id，
  不能使用 Python object identity。
- instance receiver、closure context 或 member selection 只能作为
  `ResolvedFunction.context` 的 data，不得是 callable 或 frontend executor。
- adapter 无法可靠解析时返回 `NotHandled`；不要选第一个匹配项。

`list_simulation_targets()` 只列出当前 artifact 中可唯一解析的入口 target。
它不应为了方便把每个 nested helper、合成 bridge 或无法满足参数约定的函数
都暴露给用户。需要暴露时，label 必须说明其稳定身份。

## 38. Unsupported 决策矩阵

| 情况 | frontend 行为 | 结果 |
|---|---|---|
| 输入不是本格式 | `can_load=False` | 交给其他 frontend |
| 看起来是本格式但 header 损坏 | `decode()` 抛 `FrontendDecodeError` | 用户看到 decode error |
| opcode 边界无法确定 | `decode()` 抛 `FrontendDecodeError` | 防止错位解析 |
| opcode 已知但未实现 effect | `UnknownOpcode` 或 `effects=None` | partial/unsupported |
| branch target 无效 | 提交 diagnostic，不猜 target | partial/unsupported |
| branch target 算不出 | 不提交 target hint | partial/unsupported 或线性片段 |
| 栈深度不足 | 让 core diagnostic | partial/unsupported |
| control flow 太复杂 | 正确 effects/hints + stateful callbacks | `if/goto` fallback |
| core 能安全结构化 | 正确 facts | `if/while/switch` |

开发时要避免两种危险假成功：

- 输出 `status ok`，但复杂控制流被线性化。
- 输出高级结构，但条件极性或 target 错。

这两种都比 explicit partial 更难排查。

## 39. 最小可运行外部插件清单

新作者应该先做一个 tiny VM，而不是直接上完整 VM。

最小功能：

- 一个常量 opcode。
- 一个二元运算 opcode。
- 一个输出或返回 opcode。
- 一个无条件跳转样例。
- 一个条件跳转样例。

文件清单：

```text
tiny-vm-plugin/
├── unidecompiler-plugin.toml
├── tiny_vm_frontend/
│   ├── __init__.py
│   ├── model.py
│   ├── decoder.py
│   ├── plugin.py
│   └── lifter.py
└── tests/
    └── test_integration.py
```

如果 tiny VM 还要支持模拟，增加：

```text
tiny-vm-plugin/
├── tiny_vm_frontend/
│   └── simulation.py
└── tests/
    ├── test_integration.py
    ├── test_simulation.py
    └── test_simulation_environment.py
```

验收顺序：

1. `python -m py_compile tiny_vm_frontend/*.py`
2. API 注册目录成功。
3. 线性样本输出表达式。
4. 条件跳转样本输出 `if` 或 `if/goto`。
5. 后向跳转样本 CFG 有回边。
6. GUI 能识别文件，不显示为 resource。
7. GUI CFG view 不崩溃。
8. 如果声明支持模拟，target discovery 能列出唯一函数。
9. simulator 能执行至少一个纯计算函数并保留返回值。
10. 未解析外部调用在提供 environment 时可处理，未提供时显式失败。
11. adapter 没有 frontend bytecode interpreter 或 executable callback。

文档片段可以直接复制，但必须替换：

- frontend id。
- opcode 名。
- offset 坐标系。
- operand 解码。
- 栈顺序。
- branch target 来源。
- runtime call 名称。
- simulation query 格式。
- simulation adapter 的运行时 facts。
- 外部 environment 需要处理的函数名和返回值协议。

### 39.1 完整最小插件示例

下面是一个闭合的 tiny VM 外部插件骨架。它支持：

- `CONST n`
- `ADD`
- `RETURN`
- `JUMP target`
- `JUMP_IF_ZERO target`
- `NOP`

这个例子是 immediate target + stack condition 模型。它不是某个现成 VM 的复刻，只是一个可复制的最小闭环。

更准确地说：它只适用于“跳转目标是 immediate，条件值来自栈”的 toy VM。若你的 VM 使用寄存器条件、relative target 或 jump table，请把对应部分替换掉，不要直接照抄。

```text
tiny_vm_plugin/
├── unidecompiler-plugin.toml
└── tiny_vm_frontend/
    ├── __init__.py
    ├── model.py
    ├── decoder.py
    ├── plugin.py
    └── lifter.py
```

`unidecompiler-plugin.toml`：

```toml
[frontend]
id = "tiny-stack-vm"
module = "tiny_vm_frontend.plugin:TinyStackVmFrontendPlugin"
```

`tiny_vm_frontend/model.py`：

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TinyInstruction:
    offset: int
    opcode: str
    size: int
    operands: tuple[int, ...] = ()
    raw: str = ""
    line: int | None = None


@dataclass(frozen=True)
class TinyFunction:
    name: str
    offset: int
    instructions: tuple[TinyInstruction, ...]
    params: tuple[str, ...] = ()
    local_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class TinyProgram:
    filename: str | None
    version: int
    functions: tuple[TinyFunction, ...]
    diagnostics: tuple[str, ...] = ()
```

`tiny_vm_frontend/decoder.py`：

```python
from __future__ import annotations

from .model import TinyFunction, TinyInstruction, TinyProgram

MAGIC = b"TVM1"

OP_CONST = 0x01
OP_ADD = 0x02
OP_RETURN = 0x03
OP_JUMP = 0x04
OP_JUMP_IF_ZERO = 0x05
OP_NOP = 0x00


def looks_like_tiny_vm(data: bytes, filename: str | None = None) -> bool:
    return data.startswith(MAGIC) or (filename is not None and filename.endswith(".tvm"))


def decode_tiny_vm(data: bytes, filename: str | None = None) -> TinyProgram:
    if len(data) < 6 or not data.startswith(MAGIC):
        raise ValueError("missing TVM magic")
    version = data[4]
    function_count = data[5]
    offset = 6
    functions: list[TinyFunction] = []

    for _ in range(function_count):
        if offset >= len(data):
            raise ValueError("truncated function table")
        name_len = data[offset]
        offset += 1
        if offset + name_len > len(data):
            raise ValueError("truncated function name")
        name = data[offset:offset + name_len].decode("ascii", errors="strict")
        offset += name_len
        if offset + 2 > len(data):
            raise ValueError("truncated instruction count")
        ins_count = int.from_bytes(data[offset:offset + 2], "little")
        offset += 2

        instructions: list[TinyInstruction] = []
        for _ in range(ins_count):
            if offset >= len(data):
                raise ValueError("truncated instruction stream")
            ins_offset = offset
            opcode = data[offset]
            offset += 1
            if opcode in {OP_CONST, OP_JUMP, OP_JUMP_IF_ZERO}:
                if offset >= len(data):
                    raise ValueError(f"truncated operand at {ins_offset:#x}")
                operand = data[offset]
                offset += 1
                instructions.append(
                    TinyInstruction(
                        offset=ins_offset,
                        opcode={OP_CONST: "CONST", OP_JUMP: "JUMP", OP_JUMP_IF_ZERO: "JUMP_IF_ZERO"}[opcode],
                        size=2,
                        operands=(operand,),
                        raw=f"{ins_offset:04x}: ...",
                    )
                )
            elif opcode == OP_ADD:
                instructions.append(TinyInstruction(offset=ins_offset, opcode="ADD", size=1, raw=f"{ins_offset:04x}: ..."))
            elif opcode == OP_RETURN:
                instructions.append(TinyInstruction(offset=ins_offset, opcode="RETURN", size=1, raw=f"{ins_offset:04x}: ..."))
            elif opcode == OP_NOP:
                instructions.append(TinyInstruction(offset=ins_offset, opcode="NOP", size=1, raw=f"{ins_offset:04x}: ..."))
            else:
                instructions.append(TinyInstruction(offset=ins_offset, opcode=f"OP_{opcode:02X}", size=1, raw=f"{ins_offset:04x}: ..."))

        functions.append(
            TinyFunction(
                name=name,
                offset=instructions[0].offset if instructions else offset,
                instructions=tuple(instructions),
            )
        )

    return TinyProgram(filename=filename, version=version, functions=tuple(functions))
```

一个最小输入样本：

```text
54 56 4d 31 01 01 04 6d 61 69 6e 04 00 01 02 01 03 02 03
```

这串字节的含义是：

- `TVM1` magic
- version = `1`
- function_count = `1`
- function name = `main`
- instruction_count = `4`
- instructions = `CONST 2`, `CONST 3`, `ADD`, `RETURN`

这份样本的预期输出只需要理解成“线性计算并返回”，例如：

```text
function main() {
    return 2 + 3
}
```

如果后端进一步做常量折叠，也可能显示成 `return 5`。这里关心的是控制流和栈语义是否正确，不是最终文本是否保留中间常量形式。

`tiny_vm_frontend/plugin.py`：

```python
from __future__ import annotations

from unidecompiler.core.ir import ModuleIR
from unidecompiler.plugins import FrontendDecodeError, FrontendModule, FrontendVersionSupport

from .decoder import decode_tiny_vm, looks_like_tiny_vm
from .lifter import lift_program


class TinyStackVmFrontendPlugin:
    id = "tiny-stack-vm"
    display_name = "Tiny Stack VM"
    supported_inputs = (".tvm",)
    version_support = FrontendVersionSupport(
        family="tiny-stack-vm",
        versions=("1",),
        parser="tiny-stack-vm decoder 1",
        status="experimental",
    )

    def can_load(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_tiny_vm(data, filename)

    def decode(self, data: bytes, filename: str | None = None) -> FrontendModule:
        try:
            program = decode_tiny_vm(data, filename)
        except ValueError as error:
            raise FrontendDecodeError(str(error)) from error
        return FrontendModule(
            frontend_id=self.id,
            payload=program,
            metadata={
                "filename": filename,
                "format": self.id,
                "version": program.version,
                "diagnostics": program.diagnostics,
            },
        )

    def lift(self, module: FrontendModule) -> ModuleIR:
        if module.frontend_id != self.id:
            raise TypeError(module.frontend_id)
        return lift_program(module.payload, module.metadata)
```

`tiny_vm_frontend/lifter.py`：

```python
from __future__ import annotations

from unidecompiler.core.effects import Binary, Push, ReturnTop, UnknownOpcode
from unidecompiler.core.ir import BinaryOp, Const, SourceRef
from unidecompiler.core.vm_bytecode import VMBytecodeStep
from unidecompiler.core.vm_effect_table import VMEffectTable
from unidecompiler.core.vm_function import VMFunctionSpec, lift_steps, lift_vm_step_function, recover_vm_function
from unidecompiler.core.vm_hints import VMHint
from unidecompiler.core.vm_module import assemble_vm_module
from unidecompiler.core.vm_operands import VMDecodedInstruction, VMOperand
from unidecompiler.core.vm_region import VMLinearState, VMRegionOpcodeClasses, VMStatefulCallbacks, build_hint_region_profile

from .model import TinyFunction, TinyInstruction, TinyProgram

FRONTEND_ID = "tiny-stack-vm"
CONTROL = frozenset({"JUMP", "JUMP_IF_ZERO"})
CONDITIONAL = frozenset({"JUMP_IF_ZERO"})


EFFECTS = VMEffectTable(
    opcode_attr="opcode",
    ignored=frozenset({"NOP"}),
    exact={
        "CONST": lambda _ctx, ins, src: (Push(source=src, value=Const(source=src, value=int(ins.operands[0]))),),
        "ADD": lambda _ctx, _ins, src: (Binary(source=src, op="+", semantics="static"),),
        "RETURN": lambda _ctx, _ins, src: (ReturnTop(source=src),),
    },
    fallback=lambda _ctx, ins, src: (UnknownOpcode(source=src, opcode=ins.opcode, raw=ins.raw),),
)

REGION_CLASSES = VMRegionOpcodeClasses(
    noise=frozenset({"NOP"}),
    control=CONTROL,
    jumps=CONTROL,
    forward_jumps=CONTROL,
    backward_jumps=CONTROL,
    conditional_jumps=CONDITIONAL,
)


def raw_window(instructions: tuple[TinyInstruction, ...], index: int) -> tuple[str, ...]:
    start = max(0, index - 2)
    end = min(len(instructions), index + 3)
    return tuple(ins.raw for ins in instructions[start:end])


def immediate_targets(function: TinyFunction) -> dict[int, int]:
    valid_offsets = {ins.offset for ins in function.instructions}
    targets: dict[int, int] = {}
    for ins in function.instructions:
        if ins.opcode in CONTROL:
            target = int(ins.operands[0])
            if target in valid_offsets:
                targets[ins.offset] = target
    return targets


def branch_stack_width(instruction) -> int:
    if instruction.opcode == "JUMP_IF_ZERO":
        return 1
    return 0


def branch_condition(branch, stack):
    if branch.opcode != "JUMP_IF_ZERO" or not stack:
        return None
    condition = stack[-1]
    return BinaryOp(source=condition.source, op="==", left=condition, right=Const(value=0, source=condition.source))


def make_stateful_callbacks(program: TinyProgram, function: TinyFunction):
    def lift_linear(start, end, locals_, stack):
        steps = tuple(make_step(program, ins) for ins in function.instructions[start:end])
        result = lift_steps(steps, initial_locals=locals_, initial_stack=stack)
        if result.stopped_at is not None and result.state.terminator is None:
            return None
        return VMLinearState(
            locals=result.state.locals,
            stack=tuple(result.state.stack),
            statements=tuple(result.state.statements),
            terminator=result.state.terminator,
            stopped_at=(start + steps.index(result.stopped_at)) if result.stopped_at is not None else None,
        )

    return VMStatefulCallbacks(
        initial_locals=lambda: {},
        lift_linear=lift_linear,
        branch_condition=branch_condition,
        branch_stack_width=branch_stack_width,
    )
```

这里的 `stopped_at` 只是记录线性解释停到哪一条 step，方便 core 继续恢复控制流；它不是“失败就报错”的信号本身。

```python
def make_step(program: TinyProgram, instruction: TinyInstruction, targets: dict[int, int] | None = None) -> VMBytecodeStep:
    source = SourceRef(frontend=FRONTEND_ID, offset=instruction.offset)
    operands = tuple(
        VMOperand(role="target" if instruction.opcode in CONTROL else "immediate", value=value, text=str(value))
        for value in instruction.operands
    )
    decoded = VMDecodedInstruction(opcode=instruction.opcode, source=source, operands=operands, raw=instruction.raw)
    hints: tuple[VMHint, ...] = ()
    if instruction.opcode in CONTROL and targets is not None and instruction.offset in targets:
        target = targets[instruction.offset]
        hints = (VMHint(kind="branch-target", source=source, target=target, flow="conditional" if instruction.opcode in CONDITIONAL else "unconditional"),)
        if instruction.opcode in CONDITIONAL:
            hints += (VMHint(kind="materialized-condition", source=source, detail="stack", flow="conditional"),)
    return VMBytecodeStep(opcode=instruction.opcode, source=source, decoded=decoded, raw=instruction.raw, effects=EFFECTS.effects_for(program, instruction, source), hints=hints)


def lift_function(function: TinyFunction, program: TinyProgram):
    targets = immediate_targets(function)
    steps = tuple(make_step(program, ins, targets) for ins in function.instructions)
    profile = build_hint_region_profile(steps, frontend=FRONTEND_ID, opcode_classes=REGION_CLASSES, raw_window=lambda index: raw_window(function.instructions, index))
    spec = VMFunctionSpec(name=function.name, params=function.params, frontend=FRONTEND_ID, instruction_count=len(steps), local_names=function.local_names)
    return recover_vm_function(
        spec,
        lambda: lift_vm_step_function(
            spec,
            steps,
            profile=profile,
            stateful_callbacks=make_stateful_callbacks(program, function),
            raw_window=lambda index: raw_window(function.instructions, index),
        ),
        raw=tuple(ins.raw for ins in function.instructions),
    )


def lift_program(program: TinyProgram, metadata):
    return assemble_vm_module(
        name=program.filename or "<tiny-stack-vm-program>",
        source_language=FRONTEND_ID,
        metadata={"frontend": metadata, "bytecode_format": FRONTEND_ID},
        functions=tuple(lift_function(function, program) for function in program.functions),
    )
```

这个 appendix 的目的不是教你写 Tiny VM，而是给你一个最小闭环：decoder、plugin、effect table、control hints、stateful callbacks、module assembly 都有了。真正写自己的 frontend 时，只替换模型和 opcode 语义。

## 40. 什么时候需要改 core

Frontend 不能绕过 core，但有些 VM 确实需要 core 扩展。

应该考虑改 core 的情况：

- 多个 VM 都需要同一种新 effect。
- 现有 hints 无法表达某类通用控制流事实。
- low-level CFG 能保留语义，但 core 无法安全结构化常见形状。
- backend 已有结构节点，但 core 没有恢复 pass。

不应该改 frontend 的情况：

- 为了让一个样本显示成 `while` 而手写 AST。
- 为了跳过 unsupported 而删除复杂 opcode。
- 为了 GUI 好看而丢 control edge。
- 为了当前输入可读而写业务特例。

如果不能改 core，正确底线是：

```text
保留完整 decoded instruction
提交所有能确定的 effects/hints
输出 partial 或低层 if/goto
不要输出误导性线性伪代码
```
