# 记忆检索算法详解

三种检索算法构成三级回退体系：向量检索 → FTS5 关键词检索 → 子串匹配。
前两者通过评分融合协同工作，第三者作为最终兜底。

## 一、架构总览

```
recall(query)
  ├── [第1级] HybridStore.search(query)
  │     ├── _vector_search() → vec0 cosine distance    ─┐
  │     ├── _text_search()   → FTS5 BM25                ├─ _fuse() → _apply_temporal_decay() → top_k
  │     └── 无结果时 → 空列表                             ─┘
  └── [第2级] _substring_recall() → 逐行 str.lower() in line.lower()
        └── 仅在 hybrid_store 不可用或搜索失败时触发
```

---

## 二、向量检索（Vector Search）

### 2.1 嵌入模型

```
模型: all-MiniLM-L6-v2 (SentenceTransformer)
维度: 384
大小: ~80MB
加载: 懒加载（首次搜索/索引时），失败后缓存 _model_failed=True 不再重试
```

### 2.2 索引流程

```
MEMORY.md / history.jsonl 内容
  │
  ├── 文本分块: MEMORY.md 按行切分, history.jsonl 按 entry 切分（截断到 8000 字符）
  │
  ├── model.encode(texts) → float[384] 向量
  │
  └── 写入 chunks_vec 虚拟表:
        INSERT INTO chunks_vec (rowid, embedding) VALUES (chunk_id, json(embedding))
```

chunks_vec 使用 sqlite-vec 的 `vec0` 虚拟表，底层存储为 `float[384]` 数组，支持余弦距离匹配。

### 2.3 搜索流程

```
query 文本
  │
  ├── model.encode([query]) → float[384] 查询向量
  │
  └── SELECT rowid, distance
      FROM chunks_vec
      WHERE embedding MATCH json(query_vec)
      ORDER BY distance
      LIMIT 15
```

**距离转换**：vec0 返回的 `distance` 是余弦距离（范围 [0, 2]），搜索时转换为相似度：

```
vec_sim = max(0, 1 - distance / 2)
```

| 余弦距离 | vec_sim | 含义 |
|----------|---------|------|
| 0        | 1.0     | 完全相同方向 |
| 0.5      | 0.75    | 高度相似 |
| 1.0      | 0.5     | 中等相似 |
| 1.5      | 0.25    | 低度相似 |
| 2.0      | 0.0     | 完全相反方向 |

### 2.4 核心代码

```python
# hybrid_store.py:324-336
def _vector_search(self, query, limit):
    query_embeddings = self._embed([query])
    rows = self._conn.execute(
        "SELECT rowid, distance FROM chunks_vec "
        "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
        (json.dumps(query_embeddings[0]), limit),
    ).fetchall()
    return {row[0]: row[1] for row in rows}
```

### 2.5 优缺点

- **优点**：理解语义相似性，"猫"能匹配到"猫咪"、"feline"；不依赖精确关键词
- **缺点**：需要预下载模型（~80MB）；CPU 推理有延迟（每条 ~5-20ms）；跨语言效果取决于模型训练数据

---

## 三、FTS5 关键词检索

### 3.1 引擎配置

```
引擎: SQLite FTS5
分词器: porter unicode61
  - unicode61: Unicode 6.1 规范分词（支持中文按字符切分）
  - porter: Porter 词干提取（英文：running → run, cats → cat）
排名算法: BM25
```

### 3.2 索引流程

```
文本内容
  │
  └── INSERT INTO chunks_fts (rowid, content, chunk_id)
      VALUES (chunk_id, text, chunk_id)
```

FTS5 的 `rowid` 与 `chunks` 表的 `id` 对齐（通过外部内容表方式），`chunk_id` 列标记为 `UNINDEXED` 仅用于关联查询，不参与全文索引。

### 3.3 查询构造

用户查询经过安全化处理，每个 token 加双引号后用 `AND` 连接：

```python
# hybrid_store.py:352-359
def _build_fts_query(query):
    tokens = query.strip().split()
    quoted = [f'"{t}"' for t in tokens]
    return " AND ".join(quoted)
```

示例：
```
输入: "机器学习 记忆系统"
输出: "机器学习" AND "记忆系统"
```

加引号防止 FTS5 将 token 解析为特殊语法（如 `NEAR`、`OR`），`AND` 语义确保所有词都必须匹配。

### 3.4 搜索流程

```sql
SELECT chunk_id, rank
FROM chunks_fts
WHERE content MATCH '"机器学习" AND "记忆系统"'
ORDER BY rank
LIMIT 15
```

FTS5 的 `rank` 列返回 BM25 负值（越小排名越高，如 -8.5、-3.2）。先取负转正，再经 sigmoid 归一化。

### 3.5 BM25 评分归一化

```
text_score = 1 / (1 + exp(rank / 100))
```

| BM25 rank（原始） | rank 正值 | text_score | 含义 |
|-------------------|-----------|------------|------|
| -1.0              | 1.0       | ~0.495     | 高相关 |
| -4.0              | 4.0       | ~0.490     | 较高相关 |
| -8.0              | 8.0       | ~0.480     | 中等相关 |
| -50               | 50        | ~0.378     | 低相关 |

Sigmoid 在 rank ∈ [0, 15] 区间内区分度高，超过 50 后趋于平缓，避免少数极高 rank 条目完全支配结果。

### 3.6 核心代码

```python
# hybrid_store.py:338-350
def _text_search(self, query, limit):
    fts_query = self._build_fts_query(query)
    rows = self._conn.execute(
        "SELECT chunk_id, rank FROM chunks_fts "
        "WHERE content MATCH ? ORDER BY rank LIMIT ?",
        (fts_query, limit),
    ).fetchall()
    return {int(row[0]): -float(row[1]) for row in rows}
```

### 3.7 优缺点

- **优点**：零额外依赖（SQLite 内建）；精确关键词匹配；BM25 考虑词频和文档长度归一化；Porter 词干提取处理英文形态变化
- **缺点**：不理解语义（"开心"不会匹配"快乐"）；中文分词粗糙（按字符切分）；对长查询的 AND 语义过于严格

---

## 四、子串匹配（Substring Recall）

### 4.1 算法

```
输入: query, top_n

对 MEMORY.md 的每一行:
  1. 跳过不以 "- " 开头的行（只匹配记忆条目）
  2. if query.lower() in line.lower():  匹配
  3. 解析行格式: "- [type] name: content"
     - 提取 mem_type（方括号内的类型标记）
     - 提取 name（冒号前的名称）
     - 提取 content（冒号后的内容，去掉注释 "#..." 部分）
  4. 结果数达到 top_n 时提前终止
```

### 4.2 核心代码

```python
# context_manager.py:904-932
def _substring_recall(self, query, top_n):
    current = self.store.read_memory_file()
    query_lower = query.lower()
    results = []
    for line in current.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        if query_lower not in line.lower():
            continue
        # 解析记忆行格式: "- [type] name: content"
        ...
        if len(results) >= top_n:
            break
    return results
```

### 4.3 优缺点

- **优点**：零依赖；结果可解释（所见即所得）；即时可用，无索引构建开销
- **缺点**：`O(n)` 线性扫描（n = MEMORY.md 行数）；大小写不敏感但除此之外无任何智能；不跨语言、不理解同义词；无评分排序（结果顺序 = 文件中的出现顺序）

---

## 五、评分融合（Score Fusion）

### 5.1 融合公式

```
final_score = 0.7 × vec_sim + 0.3 × text_score
```

其中：
- `vec_sim = max(0, 1 - cosine_distance / 2)` — 向量相似度
- `text_score = 1 / (1 + exp(rank / 100))` — FTS5 BM25 归一化得分

### 5.2 融合策略

```
1. 取 vec_results 和 text_results 的并集（所有出现在任意结果集中的 chunk_id）
2. 对每个 chunk_id:
   - 向量得分: 存在于 vec_results → vec_sim，否则 0.0
   - 文本得分: 存在于 text_results → text_score，否则 0.0
   - 加权求和: 0.7 × vec + 0.3 × text
3. 按融合分数降序排列
```

### 5.3 为什么是 0.7:0.3

- 向量检索提供**语义理解**能力，是主要质量来源
- FTS5 提供**精确关键词锚定**，防止向量漂移到完全不相关的文本
- 0.7/0.3 的比例参考了学术界和工业界（如 OpenClaw）的实践：语义为主、关键词为辅

### 5.4 单纯向量或无 FTS5 场景

当一种检索路径无结果时，另一路径的得分直接乘以权重生效：

```
仅向量命中:  final = 0.7 × vec_sim  （最大 0.7）
仅 FTS5 命中: final = 0.3 × text_score  （最大 ~0.15）
两者均命中:  final = 0.7 × vec_sim + 0.3 × text_score  （最大 1.0）
```

这个天然的不对称设计使得"向量+FTS5 同时命中"的结果排名显著高于"仅一种命中"的，提升了结果集整体质量。

### 5.5 核心代码

```python
# hybrid_store.py:361-385
def _fuse(self, vec_results, text_results):
    all_ids = set(vec_results) | set(text_results)
    fused = []
    for chunk_id in all_ids:
        vec_score = max(0.0, 1.0 - vec_results[chunk_id] / 2.0) \
            if chunk_id in vec_results else 0.0
        text_score = 1.0 / (1.0 + math.exp(-text_results[chunk_id] / 100.0)) \
            if chunk_id in text_results else 0.0
        final = 0.7 * vec_score + 0.3 * text_score
        fused.append((chunk_id, final))
    return fused
```

---

## 六、时间衰减（Temporal Decay）

### 6.1 衰减公式

```
decay = exp(-λ × age_days)
λ = ln(2) / 30    （30 天半衰期）
```

### 6.2 衰减曲线

| 天数 | decay 因子 | 含义 |
|------|-----------|------|
| 0    | 1.000     | 当天记忆，无衰减 |
| 7    | 0.851     | 一周后，保留 85% |
| 15   | 0.707     | 半月后，保留 71% |
| 30   | 0.500     | 一个月，半衰 |
| 60   | 0.250     | 两个月，1/4 |
| 90   | 0.125     | 三个月，1/8 |
| 365  | ~0.0002   | 一年后，近乎 0 |

### 6.3 豁免规则

```
MEMORY.md 条目 → 永久豁免衰减（evergreen）
history.jsonl 条目 → 应用指数衰减
```

设计理由：MEMORY.md 是经过 Dream 精炼的长期知识，不应随时间贬值。history.jsonl 是原始对话摘要，时效性强，旧对话的参考价值自然下降。

### 6.4 核心代码

```python
# hybrid_store.py:387-412
def _apply_temporal_decay(self, items):
    decay_lambda = math.log(2) / 30.0
    now = time.time()
    result = []
    for chunk_id, score in items:
        row = self._conn.execute(
            "SELECT source, created_at FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        source, created_at = row[0], row[1]
        if source == "memory.md":
            result.append((chunk_id, score))       # 永久豁免
        else:
            age_days = (now - created_at) / 86400.0
            decay = math.exp(-decay_lambda * max(age_days, 0))
            result.append((chunk_id, score * decay))
    return result
```

---

## 七、三级回退机制

```
recall(query)
  │
  ├── HybridStore 可用?
  │     ├── YES → search(query)
  │     │     ├── sqlite-vec 可用? → 向量 + FTS5 融合
  │     │     └── sqlite-vec 不可用 → 纯 FTS5 (text_score 不经融合直达结果)
  │     ├── 搜索异常 → 日志记录，继续回退
  │     └── 搜索结果为空 → 回退
  │
  └── 子串匹配
        └── 逐行扫描 MEMORY.md，大小写不敏感匹配
```

### 回退触发条件

| 条件 | 回退行为 |
|------|---------|
| `sentence-transformers` 未安装 | `_model_failed=True`, `_has_vec=False` → 纯 FTS5 |
| `sqlite-vec` 未安装 | `_has_vec=False` → 纯 FTS5 |
| `search.db` 创建失败 | `hybrid_store=None` → 子串匹配 |
| HybridStore.search() 抛异常 | 日志 + 子串匹配 |
| HybridStore.search() 返回空 | 子串匹配 |

---

## 八、三者对比

### 8.1 核心差异

| 维度 | 向量检索 | FTS5 关键词 | 子串匹配 |
|------|---------|------------|---------|
| **匹配方式** | 语义相似度（余弦距离） | BM25 词频-逆文档频率 | `str.lower() in line.lower()` |
| **理解 "猫" vs "猫咪"** | ✓ | ✗ | ✗ |
| **理解跨语言** | 部分支持（取决于模型） | ✗ | ✗ |
| **精确关键词** | ✗（可能漂移） | ✓ | ✓ |
| **英文形态处理** | 模型内建 | Porter 词干提取 | ✗ |
| **评分机制** | 余弦相似度 0-1 | BM25 → sigmoid 归一化 | 无（二进制匹配 + 行顺序） |
| **时间感知** | ✗ | ✗ | ✗ |
| **时间复杂度** | O(1) 索引查询 | O(log n) 索引查询 | O(n) 线性扫描 |
| **空间开销** | ~384×4×N 字节 | ~N×avg_word_len 索引 | 无 |
| **外部依赖** | sqlite-vec + SentenceTransformer | SQLite 内建 | 无 |
| **首次启动成本** | 下载 ~80MB 模型 | 无 | 无 |

### 8.2 适用场景

| 场景 | 最佳算法 | 原因 |
|------|---------|------|
| "我之前说过喜欢吃什么" | 向量检索 | 语义理解，不依赖关键词 |
| "部署脚本 deploy.sh" | FTS5 | 精确关键词匹配 |
| 搜索单个生僻缩写 | 子串匹配 | 其他两个可能都不匹配 |
| "上周讨论的那个 bug" | 向量 + 时间衰减 | 语义 + 最近优先 |
| MEMORY.md 不到 100 行 | 子串匹配 | 线性扫描也很快，不值得建索引 |

### 8.3 综合评分

| 维度 | 向量检索 | FTS5 | 子串匹配 |
|------|:---:|:---:|:---:|
| 检索质量 | ★★★★★ | ★★★☆☆ | ★☆☆☆☆ |
| 检索速度 | ★★★★☆ | ★★★★★ | ★★★☆☆(小) / ★☆☆☆☆(大) |
| 部署复杂度 | ★★☆☆☆ | ★★★★★ | ★★★★★ |
| 资源开销 | ★★☆☆☆ | ★★★★☆ | ★★★★★ |
| 可解释性 | ★★☆☆☆ | ★★★☆☆ | ★★★★★ |
| 扩展性 | ★★★★☆ | ★★★★★ | ★☆☆☆☆ |

---

## 九、配置参数

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `HYBRID_SEARCH_ENABLED` | `true` | `false` 则完全跳过 HybridStore，直接用子串匹配 |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | 任意 SentenceTransformer 兼容模型名或本地路径 |

代码常量（`memory/hybrid_store.py`）：

| 常量 | 值 | 说明 |
|------|-----|------|
| `_VEC_WEIGHT` | `0.7` | 向量路径融合权重 |
| `_TEXT_WEIGHT` | `0.3` | FTS5 路径融合权重 |
| `_HALF_LIFE_DAYS` | `30.0` | 时间衰减半衰期（天） |
| `_VEC_TOP_N` | `15` | 向量检索召回数 |
| `_TEXT_TOP_N` | `15` | FTS5 检索召回数 |
| `_DEFAULT_TOP_K` | `5` | 最终返回结果数 |
