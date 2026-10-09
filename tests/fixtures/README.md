# 合成测试样例

`synthetic_ownership_form4.xml` 完全由开发者合成，**不是 SEC 原文、真实申报或真实交易证据**。CIK、ticker、姓名、日期和金额均只用于测试。

样例验证联合申报两个主体只保留一条来源行、持仓与交易区分、衍生证券与行为代码保留、脚注不推测成价格。Form 3、修订、损坏字段和恶意 XML 由测试从这个合成样例构造。

这些测试不证明 SEC 全格式覆盖，不解决跨申报重复经济交易归并、修订行匹配、真实证券映射、交易日历或来源覆盖问题。正式获取流程仍需独立真实来源集成验证。

## Public SEC regression sample

`sec_form4_0001493152-26-041638.xml` is an unmodified official SEC response (5,250 bytes), fetched 2026-09-06 UTC. Source: https://www.sec.gov/Archives/edgar/data/1702924/000149315226041638/ownership.xml

SHA-256: `137d3059e1baf34d58b39eac18118e2f99c96e110bcd11fde9c0b515698ffdbc`. Form4 has one owner, one grant transaction and one holding row. This single sample does not validate all SEC schemas/forms or amendments.
