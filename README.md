# Atmos Paper Tracker

大气科学 / 大气遥感 / 雷达气象 论文聚合检索平台。

基于 [hehuifeng/rss_site](https://github.com/hehuifeng/rss_site) 和 [Nature_task_generate](https://github.com/hehuifeng/Nature_task_generate) 改编，根据大气科学领域自定义 RSS 源和标签。

## 覆盖期刊

| 出版社 | 期刊 | RSS Feed |
|--------|------|----------|
| EGU | Atmospheric Chemistry and Physics | `https://acp.copernicus.org/xml/rss2_0.xml` |
| EGU | Atmospheric Measurement Techniques | `https://amt.copernicus.org/xml/rss2_0.xml` |
| EGU | Geoscientific Model Development | `https://gmd.copernicus.org/xml/rss2_0.xml` |
| EGU | Earth System Science Data | `https://essd.copernicus.org/xml/rss2_0.xml` |
| EGU | Weather and Climate Dynamics | `https://wcd.copernicus.org/xml/rss2_0.xml` |
| EGU | Natural Hazards and Earth System Sciences | `https://nhess.copernicus.org/xml/rss2_0.xml` |
| EGU | Annales Geophysicae | `https://angeo.copernicus.org/xml/rss2_0.xml` |
| AGU | JGR-Atmospheres | `https://agupubs.onlinelibrary.wiley.com/feed/21698996/most-recent` |
| AGU | Geophysical Research Letters | `https://agupubs.onlinelibrary.wiley.com/feed/19448007/most-recent` |
| RMS | QJRMS | `https://rmets.onlinelibrary.wiley.com/feed/1477870x/most-recent` |
| Elsevier | Remote Sensing of Environment | `https://rss.sciencedirect.com/publication/science/00344257` |
| Springer Nature | Nature | `https://www.nature.com/nature.rss` |
| Springer Nature | Nature Climate Change | `https://www.nature.com/nclimate.rss` |

## 标签系统

- **雷达气象**：Ka/W波段雷达、双偏振、Doppler谱、反射率因子等
- **云降水物理**：riming、融化层、亮带、SLW、SIP、DSD等
- **卫星遥感**：GPM、CloudSat、CALIPSO、被动微波等
- **气溶胶-云相互作用**：CCN、INP、间接效应、气溶胶辐射强迫等
- **大气动力过程**：对流、锋面、边界层、湍流、天气尺度系统等
- **人工智能+气象**：机器学习、深度学习、预报、数据同化等

## 目录结构

```
atmos-paper-tracker/
├── index.html          # 前端入口
├── app.js              # 前端逻辑
├── styles.css          # 前端样式
├── sql-wasm.js         # sql.js 引擎
├── sql-wasm.wasm
├── data/
│   └── rss_state.db    # SQLite 数据库（浏览器端读取）
├── scripts/
│   ├── config.json     # RSS源及API配置
│   ├── rss_common.py   # 公共模块
│   ├── rss_fetch_store.py  # RSS抓取入库脚本
│   ├── backfill.py     # 回填脚本
│   ├── run_daily.bat   # Windows 每日更新脚本
│   └── ...
```

## 使用方式

### 1. 更新数据库

```bash
cd atmos-paper-tracker/scripts
set PIPELINE_CONFIG=config.json
python rss_fetch_store.py
copy rss_state.db ..\data\
```

### 2. 本地预览

```bash
cd atmos-paper-tracker
python -m http.server 3000
# 浏览器打开 http://localhost:3000
```

### 3. 部署到 GitHub Pages

将整个仓库推送到 GitHub，在 Pages 设置中选择 `main` 分支的根目录。

## 配置翻译

在 `scripts/config.json` 中填写 DeepSeek API Key：

```json
{
  "openai": {
    "api_key": "sk-xxx",
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "classifier": false
  }
}
```

然后将 `translate_on_ingest` 设为 `true`。
