# GeoNames 全球多语言行政区数据库构建工程 — 阶段一实现

从 GeoNames 数据构建「国家 → 省/州 → 市 → 区/县」四级、多语言、按国家分发的离线
SQLite 数据库包。

## 阶段一范围

- 国家：中国（CN）、日本（JP）、美国（US）
- 语言：`zh-Hans`、`ja`、`en`（+ `local` 兜底名，始终保留）
- 输出：每国一个 `build/XX.sqlite.gz` + 校验报告 + SHA-256

## 快速开始

```bash
# 1. 下载源数据（已缓存时跳过）
python3 -m location_builder.cli download

# 2. 构建全部三国（含校验、黄金样本、gzip 打包）
PYTHONPATH=src python3 -m location_builder.cli build --country ALL --index

# 3. 单独校验
PYTHONPATH=src python3 -m location_builder.cli validate --country CN

# 4. 跑测试
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

依赖：Python 3.11+、PyYAML、opencc-python-reimplemented（简繁转换）、cryptography（签名）。

## 发布产物与签名验证

```bash
# 生成 manifest + Ed25519 签名（私钥经 SIGNING_KEY 环境变量或 Actions Secret 注入）
SIGNING_KEY="$(cat signing_priv.pem)" PYTHONPATH=src python3 -m location_builder.cli manifest ALL --sign --verify

# 客户端验证签名（公钥已提交在仓库根目录 signing_pub.pem）
PYTHONPATH=src python3 -m location_builder.cli verify --key-file signing_pub.pem
```

Release 附件：`manifest.json`、`manifest.sig`、`countries.sqlite.gz`、
`CN.sqlite.gz`、`JP.sqlite.gz`、`US.sqlite.gz`、`ATTRIBUTION.txt`、`LICENSE-CC-BY-4.0.txt`。

详见 `FIX_REPORT.md`（阶段一修复记录）与 `.github/workflows/publish.yml`。

## 输出产物

| 文件 | 说明 |
| --- | --- |
| `build/CN.sqlite.gz` / `JP` / `US` | 三国压缩数据库（gzip -9） |
| `build/countries.sqlite.gz` | 全局国家索引（阶段一含 CN/JP/US） |
| `build/XX_report.json` | 结构校验 + 语言覆盖率 + 黄金样本 + 体积 + SHA-256 |
| `build/XX.sqlite` | 未压缩数据库（调试用） |

## 目录结构

```text
src/location_builder/
  cli.py        命令行（download/build/validate/diff/manifest/publish）
  downloader.py 源数据下载（缓存、重试）
  parser.py     GeoNames TSV 流式解析
  normalizer.py 层级归一化引擎（核心：映射、父子、虚拟节点、缺级兜底）
  names.py      多语言名称选择（过滤、去重、NFC、preferred）
  database.py   SQLite 写入（schema: schema/location-v1.sql）
  validator.py  结构校验 + 覆盖率 + 黄金样本
  packager.py   gzip + SHA-256
  builder.py    单国构建编排
config/countries/  国家映射配置（CN/JP/US.yaml）
config/languages.yaml 语言映射
tests/          单元 + 集成测试（黄金样本 fixtures/golden.json）
```

## 关键设计（与需求文档确认的决策）

1. **缺级兜底（按国家配置）**：`allow_missing_district` 表示单个城市允许没有区县，
   城市自然结束在 city 层（US：Los Angeles）；`self_level_fallback` 支持按层级开关
   （JP：city 开、district 关，渋谷区 → 结束在区，不生成“渋谷区 → 渋谷区”）。
2. **中国直辖市**：北京/上海/天津/重庆（GeoNames FIPS admin1 码 22/23/28/33）生成虚拟
   level2 节点，区县归 level3（北京市 → 北京市 → 朝阳区）。
3. **稳定 ID**：真实 = geoname_id；虚拟 = `2^62 | sha256(cc|level|parent|gid) % 2^62`，
   确定性、跨构建稳定、与真实 ID 永不冲突。
4. **美国 county 不物化**：城市（PPL 全量，不按人口过滤）直接挂州。
5. **中文名策略**：GeoNames 常将中文名标为 wuu/yue 而非 zh，已纳入 zh-Hans 候选源，
   并过滤非汉字书写变体。
6. **preferred 保证**：GeoNames 中文名极少带 isPreferredName，构建器为每个
   (单位, 语言) 自动指定首选名，保证客户端回退查询恒有命中。

## 性能（实测）

| 指标 | 目标 | 实测 |
| --- | --- | --- |
| 父级下子项查询 | <100ms | 0.03–5.2ms |
| 名称前缀搜索 | <200ms | 0.15–18.7ms |
| CN 压缩包 | 0.2–1.2MB | 0.5MB |
| JP 压缩包 | 0.2–1.2MB | 0.6MB |
| US 压缩包 | 1–10MB（目标） | 17.4MB（全量 16.8 万城市，真实 PPLX 区县） |

US 包体仍超目标：全量收录 16.8 万城市（产品决策，不做人口过滤）。移动端建议**按州分页、
搜索或懒加载**，不要一次加载全部城市；可选优化：人口过滤、城市合并、别名裁剪（阶段二）。

## 已知数据限制

- 美国「New York City → Brooklyn」：GeoNames 将 Brooklyn 建模为与 NYC 平级的城市
  （PPLA2），故 Brooklyn 作为 city 挂在本州（New York）下，NYC 的区县层为真实 PPLX
  （如 Greenwich Village）。
- CN 英文名覆盖率 14%、JP 英文名覆盖率 7%：GeoNames 东亚数据的 en 别名稀疏，
  客户端回退链（请求语言 → local → en → default）可兜底。

## 许可证与署名

数据 © GeoNames，CC BY 4.0。构建代码与数据许可证分离声明，见 ATTRIBUTION.md。
