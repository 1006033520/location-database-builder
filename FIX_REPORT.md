# 阶段一修复报告（2026-08-05）

依据《location-database-builder 阶段一修复需求》完成全部 12 项修复（P0×6、P1×4、P2×2），
达到 Flutter 客户端联调准入标准。

## 修复清单

| 编号 | 优先级 | 内容 | 状态 |
| --- | --- | --- | --- |
| FIX-001 | P0 | 国家索引收集全部 alternate names 后一次 select | ✅ |
| FIX-002 | P0 | 日本 district 仅取 ADM3，移除全部 PPLX | ✅ |
| FIX-003 | P0 | 美国按城市允许缺 district，禁止同名虚拟节点 | ✅ |
| FIX-004 | P0 | local 回退名称恒为 preferred | ✅ |
| FIX-005 | P0 | zh-Hans 来源优先级 + OpenCC 简繁转换 | ✅ |
| FIX-006 | P0 | CI 禁止假通过（download 失败即失败） | ✅ |
| FIX-007 | P1 | manifest + Ed25519 签名 + 发布工作流 | ✅ |
| FIX-008 | P1 | 下载 sidecar 元数据（Last-Modified/ETag/哈希） | ✅ |
| FIX-009 | P1 | normalized_name 统一 norm_key 归一化 | ✅ |
| FIX-010 | P1 | 黄金样本语义化（禁止路径/虚拟上限/特征码） | ✅ |
| FIX-011 | P2 | 版本信息参数化（config/CLI/tag 注入） | ✅ |
| FIX-012 | P2 | SOURCE_DATE_EPOCH + 固定 gzip 时间戳可重复构建 | ✅ |

## 数据对比（修复前 → 修复后）

| 指标 | CN | JP | US |
| --- | --- | --- | --- |
| 节点总数 | 3,358 → 3,358 | 9,935 → **3,087** | ~186k → 183,727 |
| level 3（区县） | 2,966 → 2,966 | 7,949（含 5,716 PPLX）→ **1,101（纯 ADM3）** | 166k+ 虚拟 → **15,148（真实 PPLX）** |
| 虚拟节点 | 28 → 28 | 1,132 → **0** | ~166,000 → **0** |
| gzip 包体 | ~510KB → 502KB | 1.5MB → **599KB** | 41.7MB → **17.4MB** |
| 结构问题 | 0 | 0 | 0 |

国家索引：CN/JP/US 均有且仅有一条 `zh-Hans`/`ja`/`en`/`local` preferred 名称
（中国/日本/美国、China/Japan/United States、アメリカ）。

## 关键语义决策

- **JP**：`日本 → 東京都 → 渋谷区` 自然结束在 level 2；政令指定都市的区（ADM3）为 level 3；
  禁止 笹塚/幡ヶ谷/松濤/代々木/恵比寿 等社区级名称出现在 district 层。
- **US**：county(ADM2) 不物化（独立产品决策，未在本轮加入）；无 district 的城市结束在
  city 层；纽约各区（PPLA2）作为 city 挂在本州下，NYC 的真实 PPLX（Greenwich Village 等）
  为 district。
- **CN**：直辖市模型保留（北京市 ADM1 → 虚拟 level2 → 朝阳区等 ADM2=level3）。

## manifest / 签名

```bash
# 生成密钥对（私钥勿入库，仅环境变量/Secret 注入）
python3 -m location_builder.cli genkey --pub signing_pub.pem --priv <私钥路径>
# 写 manifest + 签名 + 自检
SIGNING_KEY="$(cat <私钥路径>)" python3 -m location_builder.cli manifest ALL --sign --verify
# 客户端用公钥验证
python3 -m location_builder.cli verify --key-file signing_pub.pem
```

公钥已提交至仓库根目录 `signing_pub.pem`（客户端验证用）；私钥仅存在于
GitHub Actions Secret `REPO_SIGNING_KEY`。

## 已知限制（阶段二）

1. US 包 17.4MB 仍偏大（16.8 万 PPL 城市全量）；可选优化：人口过滤、城市合并、别名裁剪。
2. 美国 county 层未物化（产品决策待定）。
3. 12 语言回退链未实现（当前 zh-Hans/ja/en/local）。
4. diff 版本对比命令为占位（阶段二）。
5. 东京 23 区采用"都 → 区"两级模型；如产品需要"都 → 区部（虚拟）→ 区"需配置调整。
6. 国家索引仅覆盖 CN/JP/US 三国。

---

# 阶段一补充修复（2026-08-08）— v0.2.2

针对 v0.2.1 数据体检发现的三国重复/层级问题统一修复。

## 修复内容

### JP：城市双记录去重（PPLA2 vs ADM2）
- 问题：同一城市在 GeoNames 中同时以 ADM2（"Atsugi Shi"）与 PPLA2（"Atsugi"）出现，
  旧去重仅按 exact 名称匹配，导致 754 个 PPLA2 全部残留，level2 达 1,938 个。
- 修复：`dedupe_level2_key: admin2` —— 按 (admin1, admin2)（同一城市 code）去重，
  保留行政区实体（ADM2 权重最高），PPLA2 副本全部剔除。
- 结果：level2 1,938 → **1,190**（全部为 ADM2，无 PPLA2 残留）；北海道城市、
  东京 23 区等完整保留。

### CN：直辖市与直筒子市重复层级
- 问题 1：直辖市（北京/上海/天津/重庆）的 ADM2（"北京市"整体）被放入 level3，
  形成 北京市(省) → 北京市(虚拟市) → 北京市(区) 的三级重复。
- 问题 2：直筒子市（东莞/中山/三亚/嘉峪关等 24 个）无 ADM3 数据时生成了同名虚拟
  district（东莞市 → 东莞市）。
- 问题 3：GeoNames 双记录（ADM2 + ADM3 同城，如益阳/常德/龙岩/衡水/眉山/资阳/
  陵水/济源/仙桃/潜江/石河子/五指山）挂在省下造成重复。
- 修复：直辖市 ADM2 直接剔除（省级已代表城市整体）；`allow_missing_district: true`
  直筒子市结束在市层；`dedupe_cross_level: true` 剔除与同省 ADM2 重复的 ADM3 双记录。
- 结果：level3 2,966 → **2,926**；虚拟节点 28 → **4**（仅直辖市虚拟市）；
  北京结构 = 北京市 → 北京市 → 朝阳区/海淀区…（16 区，无"北京市"伪区）。

### US：同县同名双记录去重
- 问题：同县内 PPLA2 与 PPL 双记录（如 SC.047 Greenwood + Greenwood Village）。
- 修复：`dedupe_level2_key: admin2_name` —— 按 (admin1, admin2, 去后缀名) 去重，
  保留 PPLA2；key 含县 code，不会误删不同县的真实同名镇（AL.001/AL.035 两个 Evergreen）。
- 结果：units 183,727 → **182,941**。

## 数据对比（v0.2.1 → v0.2.2）

| 指标 | CN | JP | US |
| --- | --- | --- | --- |
| 节点总数 | 3,358 → 3,318 | 3,087 → **2,339** | 183,727 → 182,941 |
| level2 | 360 → 360 | 1,938 → **1,190** | 168,527 → 167,741 |
| level3 | 2,966 → 2,926 | 1,101 → 1,101 | 15,148 → 15,148 |
| 虚拟节点 | 28 → **4** | 0 → 0 | 0 → 0 |
| gzip 包体 | 502KB → 496KB | 599KB → **482KB** | 17.4MB → 17.4MB |

## 验证
- 单元 18/18 + 集成 10/10 全过（含 golden 语义检查）
- CN golden：中国→北京市→北京市→朝阳区 ✅；中国→安徽省→六安市→金安区 ✅
- JP golden：日本→東京都→渋谷区 ✅；禁止路径（渋谷区→笹塚 等）✅
- US golden：United States→New York→New York City→Greenwich Village ✅

## 已知限制（阶段二范围，本次未动）
- 语言覆盖：CN 的 ja/en、JP 的 zh-Hans/en、US 的 zh-Hans/ja 覆盖率低（GeoNames
  alternate names 数据限制），12 语言扩展属阶段二。
- US county 未物化（产品决策）；US 10 个无州归属的 PPL 区域（Central Coast 等）
  挂国家下，GeoNames 数据如此。
- CN 63 个挂省下的历史/省直辖县（如已撤销区县、海南/湖北省直辖县）保留挂省下，
  语义正确。
