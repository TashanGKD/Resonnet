# 科研数字分身采集系统 — 技术实现指南

## 一、核心概念：Rules 与 Skills 是什么

在 Cursor 中，有两种机制可以持久地改变 AI 的行为：

| 机制 | 文件位置 | 触发方式 | 作用 |
|:---|:---|:---|:---|
| **Rule** | `.cursor/rules/*.mdc` | 自动生效（全局或按文件类型） | 定义 AI 的「角色」和「行为准则」，始终在后台运行 |
| **Skill** | `.cursor/skills/[名称]/SKILL.md` | AI 读取后执行（用 Read 工具打开即可激活） | 提供分阶段的操作指令，相当于 AI 的「操作手册」 |

> **类比**：Rule 是 AI 的「工作职责说明书」，Skill 是具体任务的「SOP 流程手册」。Rule 让 AI 知道自己是谁、要做什么；Skill 告诉 AI 这件事具体怎么做。

---

## 二、系统架构总览

```
用户对话
    │
    ▼
[Rule: profile-collector]  ← 始终激活，感知上下文，决定调用哪个 Skill
    │
    ├─ 新建画像 ──────────► [Skill: collect-basic-info]   Phase 1: 基础身份 + 能力问答
    │                              │
    │                              ▼ 完成后询问用户
    │                       ┌──────────────────┐
    │                       │ 推断 or 填量表？  │
    │                       └──────────────────┘
    │                              │
    │                    ┌─────────┴─────────┐
    │                    ▼                   ▼
    │            [Skill: infer-    [Skill: administer-ams]
    │             dimensions]     [Skill: administer-rcss]
    │               (推断路径)    [Skill: administer-mini-ipip]
    │                    │              │       (量表路径)
    │                    └──────┬───────┘
    │                           ▼
    ├─ 审核画像 ──────────► [Skill: review-profile]   Phase 4: 展示 + 用户反馈
    │
    └─ 修改画像 ──────────► [Skill: update-profile]   随时可修改任意字段
```

**数据流转**：每个 Skill 都会读取 `profiles/[姓名].md`（用作持久化状态），完成后写回该文件。这个 markdown 文件就是整个系统的「数据库」。

---

## 三、文件目录结构

```
tashan-profile-helper/
│
├── .cursor/
│   ├── rules/
│   │   └── profile-collector.mdc          # 主控 Rule：定义画像助手的角色
│   │
│   └── skills/
│       ├── collect-basic-info/
│       │   └── SKILL.md                   # Phase 1：问答收集基础信息
│       ├── infer-profile-dimensions/
│       │   └── SKILL.md                   # Phase 2a：从基础信息推断心理维度
│       ├── administer-ams/
│       │   └── SKILL.md                   # Phase 2b：施测 AMS-GSR 28
│       ├── administer-rcss/
│       │   └── SKILL.md                   # Phase 2b：施测 RCSS
│       ├── administer-mini-ipip/
│       │   └── SKILL.md                   # Phase 2b：施测 Mini-IPIP
│       ├── review-profile/
│       │   └── SKILL.md                   # Phase 4：展示画像供审核
│       └── update-profile/
│           └── SKILL.md                   # 随时补充/修改任意字段
│
├── profiles/
│   ├── _template.md                       # 画像数据模板（空白）
│   └── [用户姓名].md                      # 每位用户的实际画像文件
│
└── doc/
    ├── tashan-profile-outline.md
    ├── academic-motivation-scale.md
    ├── researcher-cognitive-style.md
    ├── mini-ipip-scale.md
    ├── tashan-profile-examples.md
    └── implementation-guide.md            # 本文件
```

---

## 四、状态管理机制

Cursor AI 本身没有跨会话记忆。**画像文件是唯一的持久化状态**。

画像文件中有一个关键字段 `## 元信息 > 采集阶段`：

```markdown
- 采集阶段: basic_info_done | ams_done | rcss_done | mini_ipip_done | review_done
```

每个 Skill 在开始时读取此字段来判断「现在在哪个步骤」，完成后更新它，实现断点续传。

### 状态转移图

```
[空文件]
    │ collect-basic-info 完成
    ▼
[basic_info_done]
    │ 推断路径：infer-dimensions 完成
    │ 量表路径：ams + rcss + mini-ipip 全部完成
    ▼
[scales_done 或 inferred_done]
    │ review-profile 完成
    ▼
[review_done]
    │ 可随时触发 update-profile
    ▼
[updated: YYYY-MM-DD]
```

---

## 五、各组件详解

### 5.1 主控 Rule（profile-collector.mdc）

**作用**：让 AI 始终知道自己是「科研数字分身助手」，能感知用户意图并调用正确的 Skill。

**关键内容**：
- 定义角色：你是科研数字分身采集助手
- 指定各场景下应读取的 Skill 文件路径
- 规定交互语言（中文）和语气（专业但友好）
- 规定画像文件存储位置（`profiles/` 目录）

**触发时机**：`alwaysApply: true`，在此项目中始终激活。

---

### 5.2 Phase 1：基础信息采集 Skill（collect-basic-info）

**采集内容**：
1. 基础身份（研究阶段、领域、方法范式、机构）
2. 技术能力（编程语言、工具栈）
3. 科研流程能力（6个维度自评）
4. 学术网络（合作者情况）

**交互策略**：
- 分批提问（每次 2-3 个问题），避免信息过载
- 允许用户跳过不确定的字段（用「暂未填写」标记）
- 完成后询问：「接下来是否需要填写心理维度（动机/人格/认知风格）？」

---

### 5.3 Phase 2a：推断路径（infer-profile-dimensions）

**适用场景**：用户不想填量表，希望 AI 根据已有信息估算

**推断逻辑**（AI 需在 Skill 中明确说明）：
- 从研究阶段、发表经历推断成就动机水平
- 从技术栈广度推断认知风格（整合 vs 深度）
- 从机构类型和合作网络密度推断外向性
- 所有推断结果需标注「（推断，置信度：中）」以区分实测数据

---

### 5.4 Phase 2b：量表施测 Skills（3个）

每个量表 Skill 的通用流程：

```
1. 读取画像文件，确认该量表尚未完成
2. 说明量表用途和预计耗时
3. 分组呈现题目（每次 4-7 题）
4. 收集用户评分
5. 计算维度分数和综合指标（AI 自动计算）
6. 将结果写入画像文件对应字段
7. 告知用户下一步（下一个量表 或 进入审核）
```

**重要设计**：题目分批呈现，用户不需要一次性回答 28 题，避免疲劳。

---

### 5.5 Phase 4：审核 Skill（review-profile）

**呈现方式**：
- 以结构化格式展示完整画像
- 高亮「推断字段」（与实测数据区分）
- 逐维度询问：「这个描述准确吗？」

**用户反馈处理**：
- 「准确」→ 标记该维度为已审核
- 「不准确，修改为…」→ 调用 update-profile 更新
- 「我想重新填量表」→ 重新触发对应量表 Skill

---

### 5.6 随时更新 Skill（update-profile）

**触发条件**：用户说「我想修改/补充…」

**设计原则**：
- 精确定位需要修改的字段（不重新采集整个画像）
- 修改后在元信息中更新最后修改时间
- 记录修改历史（保留旧值作注释）

---

## 六、交互流程示例（用户视角）

### 场景 A：首次建立画像

```
用户：帮我建立科研数字分身

AI：好的，我是你的科研数字分身助手。我们将分几步来建立你的画像...
    【读取 collect-basic-info/SKILL.md，开始 Phase 1】

    第一步：基础身份信息
    1. 你目前处于哪个研究阶段？（博士生/博后/青椒/PI）
    2. 你的主要研究领域是什么？（一级学科 + 具体方向）
    3. 你主要采用哪种研究方法？（实验/理论/计算/数据驱动）

用户：[回答问题]

AI：[继续收集能力信息...]

    基础信息采集完成！接下来想如何获取你的动机、人格、认知风格数据？
    A. 让 AI 根据已有信息推断（快速，5分钟）
    B. 填写标准化量表（精确，约 20-30 分钟）

用户：选 B

AI：好的，我们先从学术动机量表（AMS）开始，共 28 题，分 7 批作答...
    【读取 administer-ams/SKILL.md，开始施测】
```

### 场景 B：后续补充修改

```
用户：我想更新一下我的技术栈，最近学了 Julia

AI：好的，我来更新你的技术能力部分。
    【读取 update-profile/SKILL.md，定向修改字段】
    已在技术栈中添加 Julia（★★★☆☆，新学习）。
    你对 Julia 的熟练程度大概是什么水平？
```

---

## 七、实现步骤（操作顺序）

1. **创建 `.cursor/` 目录结构**（如不存在）
2. **创建 `profiles/_template.md`**（画像数据格式模板）
3. **创建主控 Rule**（`.cursor/rules/profile-collector.mdc`）
4. **依次创建 7 个 Skill 文件**
5. **测试**：新建对话，说「帮我建立科研数字分身」，验证 Rule 自动激活
6. **迭代优化**：根据实际交互体验调整 Skill 的提问顺序和细节

---

## 八、关键局限性与应对方案

| 局限性 | 原因 | 应对方案 |
|:---|:---|:---|
| AI 跨会话无记忆 | Cursor 每次新对话重置上下文 | 画像文件作为持久化状态，每次开始前 AI 读取文件 |
| Skill 无法自动链式调用 | AI 需要被告知「下一步读哪个 Skill」 | 在每个 Skill 末尾写明「完成后告知用户可输入 X 进入下一步」 |
| 量表施测依赖用户如实作答 | 无法强制验证 | 在 Skill 中说明量表目的，建立信任感，减少社会期望效应 |
| AI 计算分数可能出错 | 复杂公式（如 RAI）有计算风险 | 在 Skill 中提供计算公式和示例，AI 逐步展示计算过程供用户核验 |
| Rule 文件体积限制 | 建议 Rule 保持 50 行内 | Rule 只做调度，详细指令放在 Skill 文件中 |

---

## 九、后续扩展方向

- **批量画像**：为整个实验室/课题组批量建立画像
- **画像对比**：比较两位研究者的维度差异，用于组队匹配
- **动态更新触发器**：学生发表论文后自动提醒更新成就动机维度
- **可视化导出**：将画像数据导出为雷达图（通过代码生成）
