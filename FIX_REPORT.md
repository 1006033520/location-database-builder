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
