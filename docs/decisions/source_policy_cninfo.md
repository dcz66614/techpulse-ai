# Source Policy Decision: cninfo (`cninfo`)

- **Source ID:** `cninfo`
- **Source Family:** `regulatory_filings`
- **Status:** Disabled (Phase 2 Source; unverified limits / unauthenticated API not available)
- **Enabled:** `false`
- **Disable Reason:** Phase 2 source; must stay enabled: false per project execution plan; web API requires Shenzhen Stock Exchange SSO authenticated account and enterprise contract; unauthenticated open REST API is not provided; robots.txt is 404; unverified limits.
- **Verification Timestamp (checked_at):** `2026-10-27T23:02:10Z`
- **Policy Snapshot ID:** `sha256-2f794a1d0aac9fe3`
- **Approval Authority:** Cd (`AUTH-04`)

## 1. Official Documentation & Evidence URLs

1. **Official Portal Index:** [http://www.cninfo.com.cn/new/index](http://www.cninfo.com.cn/new/index)
2. **Data Services API Portal:** [http://webapi.cninfo.com.cn/](http://webapi.cninfo.com.cn/)
3. **Robots Exclusion:** [http://www.cninfo.com.cn/robots.txt](http://www.cninfo.com.cn/robots.txt) (Returns HTTP 404 Not Found)
4. **SZSE Unified Authentication SSO:** `https://owssso.szse.cn/sso/login`
5. **SZSE Registration Portal:** `https://www.szse.cn/application/register/`

## 2. Allowed Endpoints & HTTP Methods

- **Allowed HTTP Method:** `not stated` (Unverified; open GET API not publicly available).
- **Allowed Base URL:** `http://webapi.cninfo.com.cn`
- **Allowed Endpoints:** `not stated` (Unverified; API documentation is gated behind authenticated Shenzhen Stock Exchange SSO login sessions).

## 3. Domain Whitelist

- `www.cninfo.com.cn`
- `webapi.cninfo.com.cn`

*Note:* All outbound requests currently blocked while `enabled: false`.

## 4. Rate Limits & Polite Pool

- **Requests per Second:** `not stated` (Unverified).
- **Requests per Minute:** `not stated` (Unverified).
- **Concurrency Limit:** `not stated` (Unverified).
- **Polite Pool Rules:** `not stated` (No public documentation; requires enterprise contract or developer portal account tier).

## 5. Required Headers & Authentication

- **User-Agent:** `not stated`.
- **Authentication:** Requires Shenzhen Stock Exchange SSO login (`owssso.szse.cn`) and user registration. Per S10 prompt instructions, agents must never sign up, never accept terms, and never call undocumented APIs.

## 6. Attribution, Caching & Redistribution Rights

- **Copyright & Ownership:** "深圳证券信息有限公司 版权所有" (Shenzhen Securities Information Co., Ltd. All Rights Reserved).
- **Disclaimer & Liability:** "免责声明：本网站竭力保证所提供的证券市场信息准确可靠，但并不担保（无论提示、默示、法定或其他形式）其准确性和完整性。任何使用方不得就全部或部分使用其中的证券市场信息作为依据对于由此而引起的任何损失向本公司提出索赔。"
- **Redistribution Rights:** `not stated`. Commercial reproduction or distribution of market announcements requires explicit agreement.
- **Caching & Retention:** `not stated`.

## 7. Prohibited Behaviors & Error Handling

- **Prohibitions:**
  - Automated calling of internal unauthenticated endpoints.
  - Bypassing SSO login or scraping web pages without authorization.
  - Calling undocumented APIs.
- **Policy Enforcement:** Source remains strictly `enabled: false`. No collector may query cninfo until explicit contract terms and API access are secured and approved by Cd (`AUTH-04`).

## 8. Verbatim Supporting Quotes

> "巨潮资讯网是深圳证券交易所信息披露官方网站，由深交所全资子公司深圳证券信息有限公司运营，平台始终致力于保护投资者的知情权和参与权，为投资者提供一站式的证券市场信息服务。"  
> — *巨潮资讯网 首页版权与介绍 (www.cninfo.com.cn/new/index)*

> "深圳证券信息有限公司 版权所有"  
> — *巨潮资讯网 页脚声明 (www.cninfo.com.cn/new/index)*

> "免责声明：本网站竭力保证所提供的证券市场信息准确可靠，但并不担保（无论提示、默示、法定或其他形式）其准确性和完整性。任何使用方不得就全部或部分使用其中的证券市场信息作为依据对于由此而引起的任何损失向本公司提出索赔。"  
> — *巨潮资讯网 免责声明 (www.cninfo.com.cn/new/index)*

> "本声明在本网站首页及相关页面发布。本网站郑重提示：任何机构或者个人进入本网站，即被视为已完全知悉、理解并接受本声明的全部内容。"  
> — *巨潮资讯网 免责声明 (www.cninfo.com.cn/new/index)*
