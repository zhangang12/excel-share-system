# -*- coding: utf-8 -*-
"""天眼查扩列结果清洗：过滤→打分→选出待补联系方式的 Top 候选"""
import csv, glob, re, json

DIR = "/Users/zhangang/Desktop/excel share/excel-share-system/sales_leads_scan"
NICHE = {"tyc_mifengjiao": "密封胶", "tyc_guanfenjiao": "灌封胶/电子胶", "tyc_daore": "导热材料",
         "tyc_jiangliao": "电子浆料", "tyc_fuji": "锂电负极材料", "tyc_tuliao": "工业涂料"}

# 现有成交客户（扩列要排除）
EXISTING = {"上海旭秸包装机械设备制造有限公司","上海英菲尼蒂科技有限公司","东莞市科路得新能源科技有限公司",
"九江捷豹药械有限公司","信惠科技（涿州）有限公司","北京泰克瑞科技有限公司","厦门市豪尔新材料股份有限公司",
"哈尔滨寒鲜食品有限公司","天津渤化化工发展有限公司","安徽三棵树涂料有限公司","安徽众旺智能装备有限公司",
"安徽康达精工新材料有限公司","安徽省三棵树涂料有限公司","山东禹王和天下新材料有限公司","山东融元康医疗科技有限公司",
"徐州新沂二维新材料技术研究有限公司","慈溪市米创机械有限公司","无锡市一轩机械制造有限公司","无锡迈克威尔自动化有限公司",
"无锡迈克斯机械制造有限公司","昆山泰威尔电子科技有限公司","杭州鑫化科技有限公司","江苏一叶兰生物医疗科技有限公司",
"江苏军航创新材料有限公司","江苏新扬新材料股份有限公司","江苏盛阳消防科技有限公司","江苏翎戴智能装备有限公司",
"江苏驰通机械制造有限公司","江阴市泽顺机械有限公司","江阴市爱达机械有限公司","浙江中科融世新材料有限公司",
"浙江弘盛药业有限公司","浙江舒康科技有限公司","深圳市善柔科技有限公司","湖北正安新材料有限公司",
"湖北海润电子科技有限公司","湖北金泉新材料有限公司","漳州市好喜来食品有限公司","皋通光电（江苏）有限公司",
"福建优立盛油脂有限公司","绍兴宝旌复合材料有限公司","苏州利福泰电子有限公司","苏州杜玛科技有限公司",
"苏州极眇科技有限公司","苏州褔诺肯机械设备有限公司","重庆诺美辰科技有限公司","长沙晶触电子科技有限公司",
"陕西君睿实业有限公司","鹤山市博安防火玻璃科技有限公司"}

BAD_NAME = re.compile(r"机械|设备|装备|自动化|泵业|阀门|仪器|机器人|机电|五金|模具|刀具|电气|环保工程|贸易|商贸|电子商务|供应链|物流")
REGION_A = {"江苏", "浙江", "上海", "安徽"}

def cap_to_wan(s):
    m = re.match(r"([\d.]+)万(.*)", s or "")
    if not m: return 0
    v = float(m.group(1)); cur = m.group(2)
    if "美元" in cur: v *= 7.1
    elif "港币" in cur or "港元" in cur: v *= 0.92
    return v

rows, seen = [], set()
for f in glob.glob(f"{DIR}/tyc_*.csv"):
    key = f.split("/")[-1].replace(".csv", "")
    if key not in NICHE: continue
    with open(f, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            name = (r.get("name") or "").strip()
            if not name or name in seen or name in EXISTING: continue
            seen.add(name)
            if r.get("regStatus") not in ("存续", "在业"): continue
            if BAD_NAME.search(name): continue
            base = (r.get("base") or "").strip()
            cap = cap_to_wan(r.get("regCapital"))
            est = (r.get("estiblishTime") or "")[:4]
            year = int(est) if est.isdigit() else 0
            score = 0
            score += 30 if base in REGION_A else 10
            score += 25 if year >= 2018 else (15 if year >= 2010 else 5)
            score += 25 if cap >= 1000 else (15 if cap >= 500 else 5)
            score += 20 if r.get("matchType") == "公司名称匹配" else (12 if r.get("matchType") == "经营范围匹配" else 6)
            rows.append({"name": name, "niche": NICHE[key], "base": base, "est": est,
                         "cap": r.get("regCapital"), "match": r.get("matchType"), "score": score})

rows.sort(key=lambda x: -x["score"])
with open(f"{DIR}/_candidates.json", "w", encoding="utf-8") as fh:
    json.dump(rows, fh, ensure_ascii=False, indent=1)
print(f"候选 {len(rows)} 家，Top18：")
for r in rows[:18]:
    print(f'{r["score"]:>3} {r["name"]}｜{r["niche"]}｜{r["base"]}｜{r["est"]}｜{r["cap"]}｜{r["match"]}')
