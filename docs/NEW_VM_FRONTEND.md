# 编写新的 VM Frontend 完整指南

本文是 `unidecompiler` 新 VM/字节码 frontend 的实施手册。目标是：你只在自己的 frontend 包中完成格式解析、操作码映射和 VM 中立事实提交，通用栈恢复、控制流结构化、AST、伪代码和诊断由 `unidecompiler` core 完成。

本文适用于 Python `.pyc`、JVM `.class`、Lua chunk、.NET CLI、WASM 之外的任意 VM。Frontend 不得为了绕过 core 的恢复困难而构造源语言 AST 或 `if`/loop/CFG。

## 1. 总体数据流

```text
文件字节
  -> FrontendPlugin.can_load()
  -> FrontendPlugin.decode()
  -> FrontendModule(payload + metadata)
  -> frontend 转换为 VM 薄层事实
  -> VMBytecodeStep(effect + hint + decoded operands)
  -> lift_vm_step_function()
  -> core 栈恢复 / region / SSA / AST
  -> DecompilerEngine 统一结果
  -> pseudocode backend / GUI / CLI
```

Frontend 私有的 decoder payload 只能在自己的包内使用。Core 可以接收 `FrontendModule.metadata`，但不能依赖 payload 类型或 frontend 私有语义。

## 2. 推荐目录结构

```text
my-vm-plugin/
├── unidecompiler-plugin.toml       # 外部目录注册 manifest
├── pyproject.toml                  # 如果作为可安装发行包
├── src/
│   └── unidecompiler_plugin_myvm/
│       ├── __init__.py
│       ├── plugin.py               # FrontendPlugin 门面
│       ├── format.py               # 文件/容器解析
│       ├── model.py                # decoder 私有模型
│       ├── support.py              # 版本支持声明
│       ├── operands.py             # VMOperand 映射
│       ├── effects.py              # VMEffectTable
│       ├── hints.py                # branch/loop/exception hints
│       └── lifter.py               # VMBytecodeStep -> core
└── tests/
    ├── test_decoder.py
    ├── test_lifter.py
    └── test_integration.py
```

外部目录通过 GUI 或公开 API 注册：

```toml
# unidecompiler-plugin.toml
[frontend]
id = "my-vm"
module = "unidecompiler_plugin_myvm.plugin:MyVmFrontendPlugin"
```

`module` 必须是 `python.module:attribute`。attribute 可以是 plugin 实例，也可以是零参数 plugin 类。目录根或 `src/` 会加入导入路径。第三方依赖需要由用户预先安装；GUI 不会执行 `pip install`。

如果发布为独立 Python distribution，应同时声明 entry point：

```toml
[project.entry-points."unidecompiler.frontends"]
my-vm = "unidecompiler_plugin_myvm.plugin:MyVmFrontendPlugin"
```

entry point 由 `FrontendRegistry.discover()` 自动发现；外部 manifest 由宿主显式注册。

## 3. FrontendPlugin 门面

```python
from unidecompiler import FrontendModule, FrontendVersionSupport


class MyVmFrontendPlugin:
    id = "my-vm"                         # 全局唯一、稳定、不随显示语言变化
    display_name = "My VM"               # GUI 展示名称
    supported_inputs = (".mvm", ".mvmc") # 仅提示信息，不负责选择逻辑
    version_support = FrontendVersionSupport(
        family="my-vm-bytecode",
        versions=("1", "2"),
        parser="my-vm parser 1.0",
        status="supported",
        notes=("little-endian module header",),
    )

    def can_load(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_my_vm(data, filename)

    def decode(self, data: bytes, filename: str | None = None) -> FrontendModule:
        module = decode_container(data, filename)
        return FrontendModule(
            frontend_id=self.id,
            payload=module,                 # 只在本 frontend 内解释
            metadata={
                "filename": filename,
                "format": "my-vm",
                "version": module.version,
                "endianness": module.endianness,
                "debug_info_present": module.debug_info is not None,
                "diagnostics": list(module.diagnostics),
                "my_vm": {"word_size": module.word_size},
            },
        )

    def lift(self, module: FrontendModule):
        if module.frontend_id != self.id:
            raise TypeError("wrong frontend module")
        return lift_my_vm_module(module.payload, module.metadata)
```

`can_load` 必须快速、无副作用；不能执行外部程序或修改输入。多个 frontend 同时返回 true 时，engine 会报告 ambiguous，而不是猜测。

`decode` 只做格式解析，允许返回 frontend 私有模型。它不能构造 `If`、`While`、`BasicBlock`、`FunctionIR` 或 AST。

`lift` 只负责将私有 decoder 模型转换成 VM 薄层事实，再调用 core VM helper。所有函数必须提交完整指令流，包括当前无法恢复的 opcode。

## 4. SourceRef 与元数据

所有可定位事实都应携带：

```python
from unidecompiler.core.ir import SourceRef

source = SourceRef(
    frontend="my-vm",
    offset=instruction.offset,
    line=instruction.source_line,
    detail=f"function={function.index}",
)
```

- `frontend`：必须等于稳定的 frontend id。
- `offset`：原始字节码偏移；没有偏移时使用 `None`，不要伪造行号。
- `line`：VM debug line（不是伪代码行号）。
- `detail`：只放 provenance/context，不放控制流决定或私有恢复指令。

推荐的 `FrontendModule.metadata` 顶层字段：`filename`、`format`、`version`、`endianness`、`debug_info_present`、`diagnostics`。frontend 专属字段放在以 frontend id 命名的子字典内。

## 5. VM 薄层 IR

薄层 IR 不是语言 AST，也不是 CFG。它描述“decoder 已经知道的中立事实”，让 core 负责推导结构。

### 5.1 VMOperand

```python
from unidecompiler.core.vm_operands import VMOperand

VMOperand(role="constant", value=constant_value, text="42")
```

`role` 的合法值：

| role | 含义 |
|---|---|
| `constant` | 常量表索引或已解析常量 |
| `local` | 局部变量槽位/名称 |
| `global` | 全局变量标识 |
| `register` | VM 寄存器 |
| `target` | branch/switch 的原始目标 offset |
| `attribute` | 属性名称 |
| `member` | 成员/字段名称 |
| `immediate` | 数字、flag、宽度等立即数 |
| `raw` | 无法归类但可展示的原始操作数 |

`value` 保留中立值，`text` 是 GUI/诊断展示文本。不要把 `value` 变成 decoder 私有对象；需要私有对象时只放在 frontend 内部，再提交稳定值。

### 5.2 VMDecodedInstruction

```python
from unidecompiler.core.vm_operands import VMDecodedInstruction

decoded = VMDecodedInstruction(
    opcode="load_const",
    source=source,
    operands=(VMOperand("constant", 3, "const[3]"),),
    raw="0008: LOAD_CONST 3",
)
```

它是一个可展示的中立 opcode 行，不执行任何效果。`raw` 必须尽可能保留原始反汇编文本，未知 opcode 也要填写。

### 5.3 VMBytecodeStep

```python
from unidecompiler.core.vm_bytecode import VMBytecodeStep

step = VMBytecodeStep(
    opcode="load_const",
    source=source,
    decoded=decoded,
    effects=MY_EFFECT_TABLE.effects_for(context, instruction, source),
    hints=tuple(hints_for(instruction, source)),
    raw=decoded.raw,
)
```

- `opcode`：稳定展示名称。
- `source`：原始 offset provenance。
- `decoded`：中立操作数和 raw 文本。
- `effects`：core 可解释的 stack/value facts；无法安全解释时必须是 `None`，并由 core 产生 unsupported/diagnostic。
- `hints`：branch/region 事实，不是结构结果。

不要丢弃未知 opcode、不要在 frontend 里提前返回 partial、不要只提交“能处理的简单函数”。

## 6. Effect：描述栈和值行为

Effect 是 core 执行的最小事实。frontend 选择 effect，不直接操作 `StackMachineState`，也不直接生成 AST。

常用类别如下：

| 类别 | Effect 示例 | 用途 |
|---|---|---|
| 值/栈 | `Push`, `Pop`, `Copy`, `DuplicateTop`, `Swap`, `Unpack` | 栈形状和常量值 |
| 局部变量 | `LoadLocal`, `StoreLocal`, `AssignValue`, `UpdateLocal`, `StoreMany` | load/store/多返回值 |
| 运算 | `Unary`, `Binary`, `Compare`, `Truthy`, `SelectValue` | 表达式和条件 |
| 属性/索引 | `LoadAttr`, `StoreAttr`, `LoadItem`, `StoreItemEffect`, `LoadIndirect` | member/index/global access |
| 容器 | `BuildArray`, `ExtendArray`, `BuildSet`, `BuildMap`, `MergeMap`, `BuildString` | aggregate literal/merge |
| 调用 | `Invoke`, `InvokeKw`, `InvokeExpanded`, `InvokeMethod`, `InvokeMember`, `BuildCall`, `CallStackArgs` | call receiver/args/returns |
| 函数值 | `MakeFunctionValue`, `CallTopAs` | closure/function reference |
| 终止 | `ReturnTop`, `ReturnVoid`, `ReturnValues`, `RaiseTop`, `ReraiseTop`, `YieldTop` | terminator/exception/yield |
| 保守回退 | `UnknownOpcode` | 保留 raw opcode 并让 core 产出诊断 |

映射必须使用 table：

```python
from unidecompiler.core.vm_effect_table import VMEffectTable
from unidecompiler.core.effects import Binary, LoadLocal, Push, ReturnTop

MY_EFFECT_TABLE = VMEffectTable(
    opcode_attr="opcode",
    exact={
        "LOAD_CONST": lambda ctx, ins, source: (Push(source=source, value=ctx.const(ins.arg)),),
        "LOAD_LOCAL": lambda ctx, ins, source: (LoadLocal(source=source, name=ctx.local_name(ins.arg)),),
        "ADD": lambda ctx, ins, source: (Binary(source=source, op="+"),),
        "RETURN": lambda ctx, ins, source: (ReturnTop(source=source),),
    },
    fallback=lambda ctx, ins, source: (UnknownOpcode(source=source, opcode=ins.opcode, raw=ins.raw),),
)


def instruction_effects(context, instruction, source):
    return MY_EFFECT_TABLE.effects_for(context, instruction, source)
```

不要让 `fallback` 静默返回空 tuple。空 tuple 只适合明确的 noise opcode；未知 opcode 应使用 `UnknownOpcode` 或返回 `None`，让 core 产生可分析的 unsupported context。

## 7. VMHint：描述控制流事实

`VMHint` 只提交事实：

```python
from unidecompiler.core.vm_hints import VMHint

VMHint(kind="branch-target", source=source, target=target, flow="conditional")
VMHint(kind="loop-backedge", source=source, target=header_offset)
VMHint(kind="case-target", source=source, target=case_offset, label="case 1")
VMHint(kind="exception-region", source=source, target=handler_offset, detail="try")
```

合法 kind：`block-boundary`、`branch-target`、`case-target`、`default-target`、`fallthrough`、`loop-backedge`、`exception-region`、`exception-handler`、`branch-value`、`materialized-condition`、`call-shape`、`aggregate-shape`。

禁止提交 `if-start`、`while-node`、`else-block` 等源结构名称。Core 根据 target、offset、opcode profile 和 stack 状态决定是否能安全结构化；无法安全结构化时保留低级 CFG/goto 或 unsupported。

## 8. Opcode 分类与 region profile

需要控制流恢复时，frontend 提交 `VMRegionOpcodeClasses` 和 `build_hint_region_profile`：

```python
from unidecompiler.core.vm_region import (
    VMRegionOpcodeClasses,
    build_hint_region_profile,
)

classes = VMRegionOpcodeClasses(
    noise=frozenset({"NOP"}),
    control=frozenset({"JUMP", "JUMP_IF_FALSE", "RETURN"}),
    jumps=frozenset({"JUMP", "JUMP_IF_FALSE"}),
    forward_jumps=frozenset({"JUMP_IF_FALSE"}),
    backward_jumps=frozenset({"JUMP"}),
    conditional_jumps=frozenset({"JUMP_IF_FALSE"}),
)

profile = build_hint_region_profile(
    steps,
    frontend="my-vm",
    opcode_classes=classes,
    raw_window=lambda offset: raw_window_by_offset(offset),
)
```

分类必须是 opcode 到中立类别的纯映射。`VMRegionProfile` 的完整 callback 能力包括 noise/control/jump/forward/backward、iterator/async iterator、conditional/null/not-null/truthy jump、target/offset、raw window 和多目标 target。只有 VM 的事实确实需要时才提供这些 callback。

不要在 callback 中创建 `If`、`While`、`Branch`、block 或 CFG。region walking、loop/branch nesting 和 fallback 都属于 core。

## 9. 线性 lift 与复杂控制流

最小 frontend 先使用完整 step stream：

```python
from unidecompiler.core.vm_function import VMFunctionSpec, lift_vm_step_function

spec = VMFunctionSpec(
    name=function.name,
    params=tuple(function.params),
    frontend="my-vm",
    instruction_count=len(function.instructions),
    local_names=tuple(function.local_names),
    metadata={"function_index": function.index},
)

def lift_my_vm_function(function, context):
    steps = tuple(make_step(context, instruction) for instruction in function.instructions)
    profile = make_profile(steps, context)
    return lift_vm_step_function(
        spec,
        steps,
        profile=profile,
        callbacks=make_region_callbacks(context),
        stateful_callbacks=make_stateful_callbacks(context),
        raw_window=context.raw_window,
    )
```

可选参数：

- `profile`：提供 VM 中立控制分类。
- `callbacks`：core region pass 需要把线性 VM slice 解释成表达式时使用；callback 只解释栈和 effect，不结构化控制流。
- `stateful_callbacks`：需要跨 block 保存 locals/stack、materialized condition、exception 或低级 CFG fallback 时使用。
- `initial_locals`、`initial_stack`：函数入口已有的中立值。
- `raw_window`：unsupported 诊断使用的原始 instruction window。

`VMLiftTable` 可用于 frontend 内的多个通用 lift 尝试，但不能把它变成针对某个业务 corpus 的恢复逃逸口：

```python
MY_LIFT_TABLE = VMLiftTable(
    rules=(
        VMLiftRule("normal", lift_normal, accept=is_safe_result),
        VMLiftRule("exception-aware", lift_exception_aware, accept=is_safe_result),
    ),
    fallback=lift_conservative,
)
```

所有尝试仍必须提交同一套 VMBytecodeStep；frontend 不得根据“简单/复杂”提前拒绝输入。

## 10. Module assembly

函数结果通过 core helper 组装模块：

```python
from unidecompiler.core.vm_module import assemble_vm_module

return assemble_vm_module(
    name=module.name,
    source_language="my-vm",
    functions=tuple(lift_my_vm_function(function, context) for function in module.functions),
    metadata=metadata,
)
```

不要从 frontend 直接调用 `assemble_module`、`assemble_function` 或构造 `FunctionIR`。`assemble_vm_module` 会把 generic FunctionIR 交给 core 的 AST/report/backend pipeline。

## 11. 错误、unsupported 与诊断

### Decoder 错误

- 输入不是该格式：`can_load=False`。
- 格式匹配但损坏：`decode` 抛 `FrontendDecodeError`，不要吞掉异常。
- 未知版本：在 frontend 的 `version_support` 和 diagnostics 中明确报告。

### Lift 错误

未知 opcode、栈深度不一致、目标不存在、exception region 不完整时：

1. 仍然提交指令和 raw 文本。
2. 使用 `UnknownOpcode` 或 `effects=None`。
3. 提供 `raw_window`、decoded operands、target/region hints。
4. 让 core 返回 `partial` 或 `unsupported` `FunctionIR`，不要猜测。

`unsupported` 不是开发终点。支持范围内出现的 unsupported 必须通过 core 修复或增加新的 VM-neutral effect/hint。只在无法安全恢复时保留 fallback。

## 12. 一个最小完整示例

下面示例假设 VM 格式是：每条指令 `{offset, opcode, arg}`，支持 `CONST`、`LOAD`、`ADD`、`RETURN`。

```python
# lifter.py
from unidecompiler.core.effects import Binary, LoadLocal, Push, ReturnTop, UnknownOpcode
from unidecompiler.core.ir import Const, SourceRef
from unidecompiler.core.vm_bytecode import VMBytecodeStep
from unidecompiler.core.vm_effect_table import VMEffectTable
from unidecompiler.core.vm_function import VMFunctionSpec, lift_vm_step_function
from unidecompiler.core.vm_operands import VMDecodedInstruction, VMOperand


class Context:
    def __init__(self, constants, locals_):
        self.constants = constants
        self.locals = locals_


MY_EFFECT_TABLE = VMEffectTable(
    opcode_attr="opcode",
    exact={
        "CONST": lambda c, i, s: (Push(source=s, value=Const(source=s, value=c.constants[i.arg])),),
        "LOAD": lambda c, i, s: (LoadLocal(source=s, name=c.locals[i.arg]),),
        "ADD": lambda c, i, s: (Binary(source=s, op="+"),),
        "RETURN": lambda c, i, s: (ReturnTop(source=s),),
    },
    fallback=lambda c, i, s: (UnknownOpcode(source=s, opcode=i.opcode, raw=i.raw),),
)


def make_step(context, instruction):
    source = SourceRef(frontend="my-vm", offset=instruction.offset)
    decoded = VMDecodedInstruction(
        opcode=instruction.opcode,
        source=source,
        operands=(VMOperand("immediate", instruction.arg, str(instruction.arg)),),
        raw=f"{instruction.offset:04x}: {instruction.opcode} {instruction.arg}",
    )
    return VMBytecodeStep(
        opcode=instruction.opcode,
        source=source,
        decoded=decoded,
        raw=decoded.raw,
        effects=MY_EFFECT_TABLE.effects_for(context, instruction, source),
    )


def lift_function(function, context):
    steps = tuple(make_step(context, item) for item in function.instructions)
    spec = VMFunctionSpec(
        name=function.name,
        params=tuple(function.params),
        frontend="my-vm",
        instruction_count=len(steps),
    )
    return lift_vm_step_function(spec, steps)
```

真实 frontend 还需要在 branch、switch、loop、exception、calls、aggregate 和 debug metadata 上提交相应 facts；这个最小示例只演示接口，不代表复杂 VM 的完整覆盖。

## 13. 测试清单

### Decoder

- 正确 header/version/endianness。
- 截断输入、错误长度、损坏常量表。
- 所有可解码 opcode 都有稳定 offset、operand 和 raw 文本。
- debug line、locals、constants、exception table、函数边界完整保留。

### Thin IR

- 每条 decoded instruction 都生成 `VMBytecodeStep`。
- `SourceRef.frontend` 和 offset 正确。
- operands 只使用上述 VM-neutral roles。
- effect table 覆盖已知 opcode；未知 opcode 有明确 fallback。
- branch/case/backedge/exception hints 的 target 正确。

### Core 集成

- 线性函数生成正确 AST/伪代码。
- if/else、loop、switch、nested region、call、return、raise、yield。
- 局部变量、闭包/upvalue、成员/索引、aggregate、multi-return。
- malformed stack、unknown opcode、invalid target 返回可定位 partial/unsupported。
- 模块内一个函数失败不会阻止其他函数。

### Decoupling guardrails

```sh
.venv/bin/python -m pytest -q tests/test_frontend_decoupling.py
.venv/bin/python -m pytest -q
```

Frontend 不得：导入 core 私有恢复实现、直接调用 effect executor、构造 AST/CFG/block/function、注册 corpus-specific lift rule、为某个 fixture 写特殊分支。

## 14. 注册、验证和发布

内置发行包：

```python
from unidecompiler import DecompilerEngine
engine = DecompilerEngine.discover()
```

外部目录：

```python
engine.register_frontend_directory("/work/my-vm-plugin")
plugin = engine.registry.get("my-vm")
engine.unregister_frontend("my-vm")  # 当前会话逻辑卸载
```

卸载不会从 `sys.modules` 删除 Python 模块，也不会破坏已有 `DecompileResult`。正在运行的 GUI 反编译任务结束后才能修改 registry。注册目录中的代码是受信任 Python 代码，宿主应在 UI 中显示来源并在首次注册时提示用户。

## 15. 常见错误

| 错误 | 后果 | 正确做法 |
|---|---|---|
| frontend 自己构造 `If`/loop/AST | core 无法统一恢复和定位 | 提交 steps/effects/hints |
| 只提交简单 opcode | 复杂输入丢失上下文 | 所有可解码 opcode 都提交 |
| 未知 opcode 返回空 effects | 产生误导性伪代码 | `UnknownOpcode`/unsupported context |
| branch target 使用伪代码行号 | CFG 错位 | 使用原始 bytecode offset |
| 把私有 payload 放进 VMOperand | core 与 frontend 耦合 | 使用 neutral value/text |
| 把 raw metadata 当控制流 | 恢复策略不可测试 | 用 `VMHint` 表达事实 |
| 在 backend 推断 loop/branch | 多 frontend 行为不一致 | 让 core region pass 结构化 |
| 用 subprocess 代替库解析 | 平台和诊断不稳定 | 使用规定的 parser/library |

完成 frontend 后，应先运行真实 source/generate stress corpus，对比逻辑；不能仅以单元测试通过作为完成标准。
