# EmojiVM 虚拟机与指令集参考文档

> 来源：反汇编 `emojivm`（ELF 64-bit, x86-64, PIE, stripped, C++）。
> 配套工具：`disasm_evm.py`（反汇编器）、`emuvm.py`（模拟器）。

---

## 1. 概述

`emojivm` 是一台**基于栈的虚拟机**，其「机器码」是一个 **UTF-8 编码的 emoji 源文件**。每个 emoji（一个 Unicode 码点）被解析成一个 `wchar_t`，作为一条指令或一个常量。

- 程序用法：`./emojivm <source_file>`
- 源文件：UTF-8 文本，无需分隔符，emoji 连续书写。
- 执行模型：逐码点解释执行，`switch` 分派到 23 种操作码（opcode 1..23）。

---

## 2. 程序执行流程

```
main()
 ├─ 检查 argc == 2，否则打印 Usage 并退出
 ├─ sub_2F1D()   初始化环境
 │    ├─ setlocale(LC_CTYPE, "en_US.utf8")
 │    ├─ setvbuf(stdin/stdout/stderr, NULL, _IONBF, 0)   # 无缓冲
 │    ├─ signal(SIGALRM=14, handler)                      # 60 秒超时
 │    └─ alarm(0x3C)
 ├─ sub_4221()   初始化两张码点映射表（见第 4、5 节）
 ├─ sub_2D2A()   读源文件：ifstream → ostringstream → codecvt_utf8 → wstring
 └─ sub_4DB8()   解释器主循环（见第 6 节）
```

---

## 3. 虚拟机状态与内存模型

### 3.1 寄存器 / 变量

| 名称 | 伪代码变量 | 含义 | 初值 |
|---|---|---|---|
| 指令指针 `ip` | `v36` | 当前码点在程序数组中的下标 | 0 |
| 栈指针 `sp` | `v34` | 数据栈栈顶下标（-1 表示空栈） | -1 |
| 步数计数 | `v37` | 已执行指令数，防死循环 | 0 |
| 程序长度 | `v38` | `wstring.length()` | — |

### 3.2 数据栈 `qword_20E260`

- 元素类型：64 位有符号整数（`__int64`）。
- 容量：1024 个（下标 0..1023）。
- 操作：`push` = `stack[++sp]`，`pop` = `stack[sp--]`。

### 3.3 缓冲区 `qword_20E200[10]`

最多 10 个缓冲区，每个是 `{ QWORD size; QWORD data; }` 结构体：

```c
struct buffer {
    uint64_t size;   // 分配大小
    uint8_t *data;   // 数据指针（分配时 size+1 字节，全零初始化）
};
```

| 操作 | 子函数 | 地址 | 说明 |
|---|---|---|---|
| 分配 | `sub_4B97` | 0x4b97 | `size <= 0x5DC`(1500)，`new char[size+1]` 清零，放入第一个空闲槽 |
| 释放 | `sub_4CCF` | 0x4ccf | 释放 `data` 与结构体，槽位置空 |
| 读字节 | `sub_4744` | 0x4744 | `mem[idx][off]`，带边界检查 |
| 写字节 | `sub_4850` | 0x4850 | `mem[idx][off] = val`，带边界检查 |
| 读 stdin | `sub_4965` | 0x4965 | `read(0, data, size)` |
| 写 stdout | `sub_4A25` | 0x4a25 | `write(1, data, strlen(data))` |

---

## 4. 指令集（操作码映射表 `unk_20E180`）

| opcode | 十进制码点 | U+ 码点 | emoji | 助记符 | 类别 |
|:---:|---:|:---:|:---:|---:|---|
| 1 | 127539 | U+1F233 | 🈳 | NOP | 控制 |
| 2 | 10133 | U+2795 | ➕ | ADD | 算术 |
| 3 | 10134 | U+2796 | ➖ | SUB | 算术 |
| 4 | 10060 | U+274C | ❌ | MUL | 算术 |
| 5 | 10067 | U+2753 | ❓ | MOD | 算术 |
| 6 | 10062 | U+274E | ❎ | XOR | 位运算 |
| 7 | 128107 | U+1F46B | 👫 | AND | 位运算 |
| 8 | 128128 | U+1F480 | 💀 | LT | 比较 |
| 9 | 128175 | U+1F4AF | 💯 | EQ | 比较 |
| 10 | 128640 | U+1F680 | 🚀 | JMP | 控制 |
| 11 | 127542 | U+1F236 | 🈶 | JNZ | 控制 |
| 12 | 127514 | U+1F21A | 🈚 | JZ | 控制 |
| 13 | 9196 | U+23EC | ⏬ | PUSH | 栈（双 emoji 指令） |
| 14 | 128285 | U+1F51D | 🔝 | POP | 栈 |
| 15 | 128228 | U+1F4E4 | 📤 | LOAD | 内存 |
| 16 | 128229 | U+1F4E5 | 📥 | STORE | 内存 |
| 17 | 127381 | U+1F195 | 🆕 | ALLOC | 内存 |
| 18 | 127379 | U+1F193 | 🆓 | FREE | 内存 |
| 19 | 128196 | U+1F4C4 | 📄 | READ | I/O |
| 20 | 128221 | U+1F4DD | 📝 | WRITE | I/O |
| 21 | 128289 | U+1F521 | 🔡 | PUTS | I/O |
| 22 | 128290 | U+1F522 | 🔢 | PRINT | I/O |
| 23 | 128721 | U+1F6D1 | 🛑 | HALT | 控制 |

---

## 5. 数字 / 常量映射表 `unk_20E1C0`

`PUSH` 指令是**双 emoji** 指令：`⏬` 后面紧跟一个「数字 emoji」，查此表得到值 0..10 压栈，然后 `ip += 2`。

| 十进制码点 | U+ 码点 | emoji | 值 |
|---:|---:|:---:|:---:|
| 128512 | U+1F600 | 😀 | 0 |
| 128513 | U+1F601 | 😁 | 1 |
| 128514 | U+1F602 | 😂 | 2 |
| 129315 | U+1F923 | 🤣 | 3 |
| 128540 | U+1F61C | 😜 | 4 |
| 128516 | U+1F604 | 😄 | 5 |
| 128517 | U+1F605 | 😅 | 6 |
| 128518 | U+1F606 | 😆 | 7 |
| 128521 | U+1F609 | 😉 | 8 |
| 128522 | U+1F60A | 😊 | 9 |
| 128525 | U+1F60D | 😍 | 10 |

> 查表失败（数字 emoji 不在表中、或未知指令 emoji）都会打印错误并 `exit(1)`。

---

## 6. 逐条指令语义

以下用 `pop()` 表示 `stack[sp--]`、`push(v)` 表示 `stack[++sp] = v`。
**注意操作数顺序**：弹栈时「栈顶」先弹出，记为 `b`，次顶记为 `a`。

| opcode | 助记符 | 精确语义 | 说明 |
|:---:|:---:|---|---|
| 1 | NOP | `ip += 1` | 空操作 |
| 2 | ADD | `b=pop(); a=pop(); push(a+b)` | 加法（可交换） |
| 3 | SUB | `b=pop(); a=pop(); push(b-a)` | 减法，**栈顶 − 次顶** |
| 4 | MUL | `b=pop(); a=pop(); push(a*b)` | 乘法 |
| 5 | MOD | `b=pop(); a=pop(); push(b%a)` | 取模，**栈顶 % 次顶** |
| 6 | XOR | `b=pop(); a=pop(); push(a^b)` | 异或 |
| 7 | AND | `b=pop(); a=pop(); push(a&b)` | 与 |
| 8 | LT | `b=pop(); a=pop(); push(b<a?1:0)` | 小于比较，**栈顶 < 次顶** |
| 9 | EQ | `b=pop(); a=pop(); push(b==a?1:0)` | 相等比较 |
| 10 | JMP | `ip = pop()` | 无条件跳转（目标 = 栈顶） |
| 11 | JNZ | `t=pop(); c=pop(); ip = c?t:ip+1` | 非零跳转 |
| 12 | JZ | `t=pop(); c=pop(); ip = c?ip+1:t` | 为零跳转 |
| 13 | PUSH | `push(DIGIT[code[ip+1]]); ip += 2` | 压入常量（后跟数字 emoji） |
| 14 | POP | `sp -= 1` | 丢弃栈顶（`sp==-1` 时报错） |
| 15 | LOAD | `idx=pop(); off=pop(); push(mem[idx][off])` | 读缓冲区字节 |
| 16 | STORE | `idx=pop(); off=pop(); val=pop(); mem[idx][off]=val` | 写缓冲区字节 |
| 17 | ALLOC | `size=pop(); 分配 size 字节缓冲区` | 分配（`size>1500` 报错） |
| 18 | FREE | `idx=pop(); 释放缓冲区[idx]` | 释放 |
| 19 | READ | `idx=pop(); read(0, mem[idx].data, mem[idx].size)` | 从 stdin 读入 |
| 20 | WRITE | `idx=pop(); write(1, mem[idx].data, strlen(...))` | 输出缓冲区（到 `\0`） |
| 21 | PUTS | 反复 `pop()` 并输出低字节，直到弹出 0 或栈空 | 弹栈按字节输出 |
| 22 | PRINT | `v=pop(); wcout << v` | 按整数输出栈顶 |
| 23 | HALT | `return 0` | 停机 |

### 6.1 跳转指令详解

`JMP/JNZ/JZ` 弹两个值：**栈顶是跳转目标 `t`，次顶是条件 `c`**。使用时应「先压条件，再压目标」：

```
PUSH <条件>
PUSH <目标>
JNZ        ; 若 条件 != 0，ip = 目标；否则 ip += 1
```

- `JMP`：只弹一个值（目标），`ip = 目标`。
- 目标地址是**码点下标**（与 `wstring` 下标一致，等价于反汇编输出里的 `ip` 列）。

### 6.2 内存指令操作数顺序

`LOAD / STORE` 的弹栈顺序是「**偏移量在下，缓冲区号在上**」：

- LOAD：`push <偏移>; push <缓冲区号>; LOAD` → `push(mem[缓冲区号][偏移])`
- STORE：`push <值>; push <偏移>; push <缓冲区号>; STORE` → `mem[缓冲区号][偏移] = 值`

### 6.3 常量构造惯用法

程序内没有「多字节整数常量」指令，大整数用**十进制逐位拼出**：

```
PUSH a ; PUSH 10 ; MUL ; PUSH b ; ADD     →  a*10 + b
```

例如 `PUSH 1; ×10; ×10; PUSH 5; ×10; PUSH 2; ADD; ADD` 构造出 `152`。

---

## 7. 解释器主循环（伪代码）

```c
// sub_4DB8 的核心，简化表示
sp = -1;
for (ip = 0; ip < program_length; ) {
    if (++steps > 1000000)      error_exit();          // 步数上限
    op = INSN_MAP[ program[ip] ];                      // 查操作码，未命中=0→default
    switch (op) {
        case  1: ip++; break;                          // NOP
        case  2: push(pop()+pop()); ip++; break;       // ADD
        case  3: { b=pop(); a=pop(); push(b-a); ip++; }// SUB
        case  4: push(pop()*pop()); ip++; break;       // MUL
        case  5: { b=pop(); a=pop(); push(b%a); ip++; }// MOD
        case  6: push(pop()^pop()); ip++; break;       // XOR
        case  7: push(pop()&pop()); ip++; break;       // AND
        case  8: { b=pop(); a=pop(); push(b<a); ip++; }// LT
        case  9: push(pop()==pop()); ip++; break;      // EQ
        case 10: ip = pop(); break;                    // JMP
        case 11: { t=pop(); c=pop(); ip = c ? t : ip+1; } break; // JNZ
        case 12: { t=pop(); c=pop(); ip = c ? ip+1 : t; } break; // JZ
        case 13: push(DIGIT[program[ip+1]]); ip += 2; break;     // PUSH
        case 14: sp--; ip++; break;                    // POP
        case 15: { idx=pop(); off=pop(); push(loadb(idx,off)); ip++; } break; // LOAD
        case 16: { idx=pop(); off=pop(); val=pop(); storeb(idx,off,val); ip++; } break;
        case 17: alloc(pop()); ip++; break;            // ALLOC
        case 18: free(pop()); ip++; break;             // FREE
        case 19: readbuf(pop()); ip++; break;          // READ
        case 20: writebuf(pop()); ip++; break;         // WRITE
        case 21: while (stack non-empty && top != 0) write_byte(pop()); ip++; break; // PUTS
        case 22: wcout << pop(); ip++; break;          // PRINT
        case 23: return 0;                             // HALT
        default: error_exit();                         // 未知 emoji
    }
}
```

---

## 8. 约束与限制

| 约束 | 数值 / 行为 |
|---|---|
| 运行超时 | `alarm(60)`，超时后打印消息并 `exit(1)` |
| 步数上限 | 1,000,000 步，超出即 `exit(1)` |
| 数据栈容量 | 1024（`sp == 1024` 时 push 报错） |
| 数据栈下溢 | `sp == -1` 时 POP 报错 |
| 缓冲区数量 | 最多 10 个（无空闲槽时 ALLOC 失败） |
| 缓冲区大小 | 单个 `size <= 1500`（0x5DC） |
| 缓冲区访问 | 下标越界 / 未分配即 `exit(1)` |
| 未知指令 / 数字 | 打印错误并 `exit(1)` |

---

## 9. 附录：C++ 库相关结构说明

- 反汇编中大量 `// attributes: thunk` 的 `_Z...` 函数是 C++ 标准库符号的 **PLT 桩**（`operator new`、`std::wstring`、`std::map`、`wstring_convert` 等）。
- 两张映射表 `unk_20E180` / `unk_20E1C0` 都是 `std::map<int,int>`；`_Rb_tree_*` 系列函数是红黑树（`std::map`）的底层实现。
  - `sub_5CB4` / `sub_5DDC` ≈ `std::map::operator[]`（插入 / 访问）
  - `sub_6108` / `sub_468F` ≈ `std::map::find`（查找，未命中返回 end → 报错）
- 源文件读取链：`std::ifstream` → `std::ostringstream` → `std::wstring_convert<std::codecvt_utf8<wchar_t>, wchar_t>`，把 UTF-8 字节流按码点解码成 `wstring`（`surrogate pair` 会被合并为单个 32 位码点）。

---

## 10. 示例：Hello 的写法（示意）

按第 6 节语义，输出字符串可用「栈底先压 0 终止符，再逆序压字符，最后 PUTS」：

```
⏬ 😀              # PUSH 0          —— 终止符（栈底）
⏬ 😁              # PUSH 1          —— 仅示意，实际应压 ASCII 码点
...               # （示例略）
🔡                # PUTS            —— 弹栈逐字节输出
🛑                # HALT
```

> 说明：`PUSH` 只能压 0..10 的数字，实际编程时字符值需用 `×10+digit` 惯用法构造；本示例仅示意 PUTS 的栈布局，非可运行代码。
