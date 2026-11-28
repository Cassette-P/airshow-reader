# Sinica Podcast 主播与专业术语配置（sinica.md）

## 固定主播与嘉宾归一化规则（Speaker）

- 固定主播标准英文名：`Kaiser Kuo`（中文译文中人名直接保留 `Kaiser` 或 `Kaiser Kuo`，不要生硬音译为“凯泽·郭”）。
- 每期飞行嘉宾：必须优先从本集提供的 **Episode Show Notes** 中提取嘉宾的准确英文全名作为 `speaker` 标签（例如 `Evan Feigenbaum`、`Joe Studwell`、`Dan Wang`），严禁在简介已给出姓名时偷懒标为 `Guest`；仅在确实无法辨认的多人插话场景使用 `Guest`。
- **强制去除头衔前缀**：提取嘉宾英文名时，**必须去除 `Dr.`、`Prof.`、`Professor`、`Ambassador` 等学术、职业或外交头衔前缀**（例如必须统一标记为 `Ruby Wang`，严禁标为 `Dr. Ruby Wang`）。
- **欧美学者与被引述人物人名强约束**：中文译文中，**不仅当期受访主嘉宾，凡对话中引述、讨论或推荐的欧美学者、作家、记者、智库专家与外交官人名，一律直接保留英文原名**（如 `Wolf Ladejinsky`、`John Mellor`、`Dame Barbara Woodward`、`Jonathan Franzen`、`Mohsin Hamid`、`Wallace Stegner`、`Dan Kurtz-Phelan`、`Stephen Platt` 等，严禁音译为“沃夫·拉德津斯基”、“芭芭拉·伍德沃德”等中文串字）；仅马克思、亚当·斯密、马克·吐温、尼克松、奥巴马、特朗普等极少数家喻户晓的历史与元首级人物可使用通行中文名。
- **中国本土人物与地名汉字还原强约束**：英文口播中以拼音提及的中国历史/政治人物、本土学者、媒体人及地名，在中文译文中**必须还原为标准汉字**（如 `Wang Zichen` → `王子辰`、`Wang Feng` → `王丰`、`Yu Xinyan` → `余心妍`、`Feng Guifen` → `冯桂芬`、`Liangzhu` → `良渚`、`Tuanjiehu` → `团结湖`），严禁在中文句子中直接裸留汉语拼音（注：`speaker` 字段仍统一使用英文名如 `Wang Zichen`、`Wang Feng`）。

## 中国议题与中美政经“回译还原”术语表

本节目讨论大量中国政治、经济、科技与社会议题，英文口播中的中国固有概念与高频地缘词汇必须**精准还原为地道中文原词**，严禁逐字机翻：

- **宏观经济与产业**：Involution（内卷）、Lying Flat（躺平）、Common Prosperity（共同富裕）、Dual Circulation（双循环）、New Quality Productive Forces（新质生产力）、Overcapacity（产能过剩）、De-risking（去风险）、Decoupling（脱钩）、SOE（国有企业 / 国企）、LGFV（地方政府融资平台 / 城投平台）、Industrial Policy（产业政策）、Industrial Party（工业党）、Supply Chain Resilience（供应链韧性）、EV（新能源汽车 / 电动车）、Made in China 2025（“中国制造2025”）、Belt and Road Initiative / BRI（“一带一路”倡议）、Three Red Lines（三道红线）、K-shaped Economy（K型经济 / K型分化）、Biologics（生物制剂）、Out-licensing（对外授权许可）、Patent Cliff（专利悬崖）、VBP（集中带量采购 / 集采）
- **关键资源、科技与管制**：Rare Earth Elements / REE（稀土元素）、Export Control（出口管制）、Chokepoint（卡脖子）、BIS（美国商务部工业与安全局）、AIGC（生成式AI）、Vibe Coding（氛围编程）
- **发展与经济体制**：Developmental State（发展型国家）、Washington Consensus（华盛顿共识）、Performance Legitimacy（政绩合法性）
- **地缘政治与印太安全**：CPTPP、CFIUS（美国外国投资委员会）、Quad（四方安全对话）、AUKUS、ASEAN Centrality（东盟中心地位）、Strategic Autonomy（战略自主）、Middle Power（中等强国）、Weaponized Interdependence（武器化相互依赖）、Global South（全球南方）、Wolf Warrior Diplomacy（战狼外交）
- **政治与机构**：PRC（中华人民共和国 / 中国大陆）、CPC / CCP（中国共产党 / 中共）、PLA（中国人民解放军）、Third Plenum（三中全会）、Two Sessions（两会）、14th / 15th Five-Year Plan（“十四五” / “十五五”规划）、NDRC（国家发改委）、PBOC（中国人民银行 / 央行）、MOFCOM（商务部）、NPC（全国人大）、CPPCC（全国政协）、Politburo Standing Committee（中央政治局常委会）
- **社会与文化**：Hukou（户籍制度 / 户口）、Danwei（单位）、Gaokao（高考）、Guochao（国潮）、Shanzhai（山寨）、Netizen（网民）、Little Pink（小粉红）、Soft Power（软实力）、Think Tank（智库）

## 翻译文风与校对红线

- 文风对标《财新》/《经济学人·商论》的深度政经访谈：冷静、克制、保留原话的思辨层次与学术/政策细微差别，不夸大、不添油加醋。
- **单集缩写一致性红线**：单集内高频出现的商业/科技缩写词（如 `OPC` 一人公司），全篇译名必须前后统一（统一使用“一人公司”），不得中途切换（如忽而“一人公司”忽而“单人公司”）。
- 节目中提及的书籍、论文、智库报告与专栏名，保留英文原名或加注公认官方中文书名，**严禁胡乱自造译名或与同集译文自相矛盾**（如 *Exit West* 统一采用公认译名《离开西方》，严禁自造“西退”）；同步校对 ASR 书名冠词/单复数误差（如 Ann Patchett 小说为 *Whistler* 而非 *The Whistlers*），并在 `vocab_notes.term` 中直接纠偏拼写误差（如 `coterminous` 而非 `co-terminus`）。
- 英文缩写与专有概念保持规范格式，全篇统一采用规范简体中文，严禁同音错别字。
- **单集书名一致性与片头/片尾订阅导流过滤红线**：同一集内多次提及同一本书时，中文译名或英文原名标注必须前后严格统一（如 *Teach the People* 统一作 `《教民》（Teach the People）`、*Home* 统一作 `《家园》（Home）`）；开场或片尾独立成段的 `sinicapodcast.com` / `Substack` 付费会员订阅号召及联名专栏导流口播一律跳过不输出（若末尾粘连正片开场白如 `"My guest today is..."`，须从正片开场句起完整保留）；严禁在片尾纯音乐（Outro Music）段凭记忆幻觉编造旧版开场白（如已停运的 `produced by The China Project`）。
