# Research and Design Blueprint for an AI Multi-Agent Trading House

## Executive assessment

A multi-agent system can credibly reproduce many functions of a professional trading house: market-data ingestion, technical and fundamental research, strategy generation, portfolio construction, execution, risk control, trade monitoring, post-trade analysis and operational surveillance. Recent systems such as TradingAgents and ContestTrade demonstrate how specialised analyst, research, risk and trading agents can collaborate or compete before producing a portfolio decision. The concept is technically viable, but the evidence that current large-language-model agents can autonomously produce durable live-trading alpha remains weak. A 2026 review of 77 agentic-trading studies found that only 19 met basic closed-loop trading criteria; only two of those reported usable time-consistent data splits, only one explicitly modelled transaction costs, only one documented survivorship handling, and none reached the review’s highest reproducibility level. citeturn6view3turn6view2turn6view1

The proposed target of **100% profit every five trading days cannot be treated as an engineering requirement or reliable performance objective**. Doubling capital every five trading days across approximately 252 trading days would compound to roughly:

\[
2^{252/5}\approx 1.49\times10^{15}
\]

times the starting capital in one year. This does not account for trading costs, liquidity constraints, market impact, taxes, leverage limits, strategy capacity, losses or changing market regimes. A backtest that repeatedly produces such a result should therefore trigger an automatic presumption of data leakage, incorrect price alignment, unrealistic leverage, omitted costs or overfitting until independently disproved.

The real-world base rates are also unfavourable. The United States Commodity Futures Trading Commission says roughly two-thirds of customers at registered over-the-counter forex dealers lose money after financing, fees and other expenses, while the Securities and Exchange Commission warns that many day traders suffer severe losses in their first months. These figures do not prove that every automated strategy must fail, but they show why a fixed doubling target is incompatible with responsible system design. citeturn0search3turn0search2

The correct objective is not to eliminate risk or “fear” risk. It is to take **measured, bounded and statistically justified risk** while preventing any agent, model or strategy from risking the survival of the trading operation. The system should optimise a hierarchy of outcomes:

| Priority | Objective |
|---|---|
| Survival | Avoid ruin, catastrophic leverage, uncontrolled losses and operational failure |
| Integrity | Ensure every signal, order, fill, model version and data snapshot is reproducible |
| Net edge | Produce positive expectancy after spread, slippage, commission, funding, borrowing, latency and market impact |
| Robustness | Perform acceptably across different volatility, liquidity and macroeconomic regimes |
| Scalability | Increase capital only where live evidence supports additional capacity |
| Return | Maximise risk-adjusted net returns within the approved risk budget |

The resulting design should be an **AI-assisted quantitative trading house**, not an unconstrained group of language models with direct access to capital. LLMs are best used for research, news interpretation, hypothesis generation, adversarial debate, coding assistance and post-trade explanation. Position sizing, portfolio limits, stop placement, order validation, reconciliation and emergency shutdowns should be controlled by deterministic software that can override every AI-generated instruction. This modular division is supported by recent research arguing that LLMs should operate as auditable information interfaces upstream of independent calibration, risk and execution modules rather than as the final authority over orders. citeturn7view0turn7view1turn7view2

## Trading-house operating model

The system should operate through clearly separated desks, each with defined authority and restricted tools. Specialisation is useful because market research, portfolio construction, execution and risk control are different problems. However, adding more agents does not automatically create independent expertise: agents based on similar models, data and prompts may produce correlated errors or reinforce one another’s biases. The architecture must therefore measure disagreement, error correlation and the incremental net contribution of each agent rather than assuming that debate improves decisions. citeturn7view1turn7view2

| Agent or service | Core responsibility | Permitted authority | Required output |
|---|---|---|---|
| **Chief investment officer agent** | Coordinates the trading session, selects active strategy families and resolves competing proposals | May recommend portfolio targets but cannot bypass risk controls | Portfolio proposal, confidence decomposition and decision rationale |
| **Market-data steward** | Validates quotes, trades, bars, order books, corporate actions, funding rates and economic calendars | May quarantine instruments or data feeds | Point-in-time data snapshot, quality score and anomaly report |
| **Regime-detection agent** | Classifies volatility, trend, liquidity, correlation and risk-on/risk-off conditions | May enable or disable strategy classes | Regime probabilities and uncertainty bands |
| **Macro and fundamental analyst** | Analyses monetary policy, economic releases, company filings, earnings and valuation information | Research only | Structured factors with source timestamps |
| **Technical analyst** | Calculates trend, breakout, volatility, support, resistance and momentum features | Research only | Deterministic indicator vector and candidate setups |
| **Microstructure analyst** | Analyses spread, order-book imbalance, trade flow, queue conditions and short-horizon liquidity | Research only | Entry-quality and execution-cost forecast |
| **News and sentiment analyst** | Extracts event type, surprise, entity, polarity and novelty from contemporaneous text | Research only | Schema-bound event features, not free-form trade sizes |
| **Strategy research agents** | Generate testable strategies and variations | May submit research specifications only | Hypothesis, economic rationale, pseudocode and invalidation conditions |
| **Bull and bear challengers** | Produce opposing interpretations of each proposed trade | Research only | Evidence-for, evidence-against and missing-information report |
| **Strategy allocator** | Weights approved strategies using expected edge, regime fit and diversification | May recommend target exposures | Target weights and attribution |
| **Independent risk officer** | Enforces portfolio, leverage, liquidity, drawdown and operational limits | Absolute veto over all agents | Approved, resized or rejected order set |
| **Execution agent** | Converts target positions into broker-compatible orders | May submit only risk-approved orders | Order plan, expected cost and routing decision |
| **Trade-monitoring agent** | Tracks fills, exposure, stops, profit protection, spread and market state | May tighten risk but may not loosen hard limits without approval | Position state and proposed stop modification |
| **Post-trade analyst** | Attributes profit and loss to signal, sizing, timing, slippage and market movement | No trading authority | Daily attribution and failure analysis |
| **Validation and red-team agents** | Search for leakage, overfitting, hidden leverage and unrealistic assumptions | May block strategy promotion | Validation report and reproducibility bundle |
| **Compliance and audit agent** | Checks instrument eligibility, jurisdiction, recordkeeping and prohibited behaviour | Absolute veto where rules are violated | Compliance decision and immutable audit record |
| **Site-reliability and security service** | Monitors infrastructure, credentials, latency, time synchronisation and broker connectivity | May cancel orders, flatten positions or halt the system | Operational status and incident record |

A productive multi-agent hierarchy should contain both **collaboration and competition**. TradingAgents uses specialised fundamental, sentiment, technical, bull, bear, trader and risk roles, while ContestTrade introduces a “Quantify–Predict–Allocate” competition in which agents are scored after outcomes become observable and future resources are assigned to agents with positive predicted usefulness. The latter is preferable to permanently trusting an agent merely because it once performed well. citeturn6view3turn6view2

The recommended decision flow is:

```text
Point-in-time market snapshot
        ↓
Data validation and instrument eligibility
        ↓
Regime classification
        ↓
Parallel specialist analysis
        ↓
Bull case ↔ bear case ↔ independent critic
        ↓
Approved-strategy signal engines
        ↓
Probability calibration and expected-cost model
        ↓
Portfolio construction
        ↓
Independent risk veto and resizing
        ↓
Deterministic execution engine
        ↓
Fill reconciliation and live trade monitoring
        ↓
Profit protection, exits and emergency controls
        ↓
Post-trade attribution and agent scoring
        ↓
Research memory and strategy review
```

The CIO agent should never decide by majority vote alone. It should require explicit supporting evidence, disagreement statistics, data freshness, estimated transaction costs and the historical reliability of each agent in the current regime. A minority agent that has proved useful in high-volatility conditions should not be suppressed merely because most other agents agree with one another. TrustTrade, for example, reports that selective credibility-weighted consensus can improve stability, but also acknowledges that consensus can suppress minority-but-correct evidence in fast-changing markets. citeturn6view4

Every message exchanged between agents should be schema-bound. A trade proposal should contain fields such as instrument, direction, time horizon, entry condition, invalidation point, expected return distribution, expected cost, required liquidity, maximum holding time, applicable strategy version and supporting data timestamps. Free-form narrative may accompany the record, but it must not replace the numerical fields used by risk and execution services.

## Strategy portfolio for scalping and swing trading

No single scalping strategy will reliably work across forex, crypto and equities. The trading house should maintain a **portfolio of independent or weakly correlated strategies**, with the regime agent enabling only those suited to current volatility, liquidity and market structure. Momentum, mean reversion and pairs trading have substantial academic histories, but historical profitability does not guarantee implementation profitability, particularly after transaction costs and changing market conditions. Research on pairs trading and momentum illustrates that these are legitimate strategy families, while other work shows that fast-reversion signals can deteriorate rapidly as transaction costs increase. citeturn4search3turn4search4turn4search34

| Strategy family | Typical horizon | Suitable markets | Core signal | Important failure mode |
|---|---:|---|---|---|
| **Order-flow momentum scalp** | Seconds to minutes | Liquid crypto, major equities, selected forex venues | Persistent imbalance between aggressive buyers and sellers, supported by depth and trade flow | False imbalance, queue-position error, spoofing, latency and spread widening |
| **Liquidity-reversion scalp** | Seconds to minutes | Highly liquid instruments | Short-lived price displacement from microprice, VWAP or local fair value | Price displacement may represent informed flow rather than noise |
| **Opening-range breakout** | Minutes to hours | Equities, indices, major forex sessions | Breakout from an initial range with volume and volatility confirmation | Opening volatility, slippage and false breakouts |
| **Intraday momentum** | Minutes to one session | Equities, indices, liquid crypto | Early-session direction, volume surprise and trend persistence | Reversal days, event shocks and overcrowding |
| **Event-driven scalp** | Seconds to minutes | Forex, equities, indices, crypto | Difference between released information and consensus expectations | Feed latency, interpretation error and immediate spread expansion |
| **Volatility-breakout swing** | Hours to days | All three asset classes | Breakout from volatility compression with trend confirmation | Failed breakout and volatility collapse |
| **Time-series momentum** | Days to weeks | Forex, crypto, equities and indices | Direction of past excess returns or trend filters | Momentum crash and abrupt regime reversal |
| **Mean-reversion swing** | Hours to days | Range-bound instruments | Standardised deviation from dynamic fair value | Structural repricing mistaken for temporary deviation |
| **Pairs or basket trading** | Hours to weeks | Related equities, crypto pairs and some forex crosses | Cointegrated or economically linked spread divergence | Relationship breakdown, borrowing constraints and asymmetric liquidity |
| **Cross-sectional momentum** | Days to weeks | Equity and crypto universes | Long relative winners and short relative losers | Hidden factor concentration and turnover costs |
| **Carry and macro trend** | Days to months | Forex and selected derivatives | Rate differential, curve structure and macro regime | Funding shocks, central-bank surprises and crowded positioning |
| **Funding or basis strategy** | Minutes to days | Crypto spot and derivatives | Difference between spot, futures, perpetual funding and borrowing costs | Exchange default, liquidation, transfer delays and basis widening |

Intraday-momentum research has found predictability in certain equity-market windows, and historical pairs-trading studies found positive relative-value results in earlier samples. These findings justify research programmes, not immediate deployment. Each strategy must be re-estimated using the exact assets, broker, order types, latency and costs that the live system will encounter. citeturn4search29turn4search1turn4search3

Scalping and swing trading should be separated operationally:

| Dimension | Scalping desk | Swing desk |
|---|---|---|
| Decision frequency | Seconds to minutes | Hours to days |
| Primary data | Trades, quotes, order books, short bars | Bars, macro data, news, filings and cross-asset factors |
| Appropriate AI role | Regime control, event extraction and post-trade analysis | Deeper research, thesis generation and event interpretation |
| Signal computation | Deterministic statistical or machine-learning models | Statistical, machine-learning and structured LLM-derived features |
| Execution priority | Latency, spread, queue and fill probability | Price impact, participation and overnight/event risk |
| Main costs | Spread, fees, adverse selection and latency | Spread, borrowing, funding, gaps and opportunity cost |
| LLM in direct order loop | No | Only behind deterministic validation and risk controls |

LLMs should not be asked to verbally inspect every tick and make sub-second trading decisions. Recent analysis distinguishes financial narrative fluency from accurate numerical execution and notes that language confidence is not equivalent to a calibrated return probability. Scalping decisions should therefore be produced by numerical models running close to the market-data and execution layers; AI agents can select regimes, investigate anomalies and adjust approved configurations at a slower cadence. citeturn7view0

Each strategy specification should include:

| Required item | Example |
|---|---|
| Economic rationale | Forced liquidity taking temporarily displaces price from estimated fair value |
| Universe | Major forex pairs with median spread below a predefined threshold |
| Trading horizon | Thirty seconds to five minutes |
| Entry rule | Imbalance, price displacement and volatility filter all exceed calibrated thresholds |
| Exit rule | Fair-value convergence, time stop, protective stop or regime change |
| Cost model | Spread, commission, slippage, latency and adverse-selection estimate |
| Capacity model | Maximum order as a fraction of visible and expected market depth |
| Invalidation | Edge disappears in rolling out-of-sample monitoring |
| Regime constraints | Disabled during stale feeds, extreme spread or specified event windows |
| Versioning | Immutable code, data and parameter identifiers |

The strategy allocator should use net expected return, uncertainty and correlation rather than allocating to whichever strategy has the highest recent return. It should also apply a strategy “half-life”: when live performance drifts away from the validated distribution, capital should automatically decay unless new evidence supports continued deployment.

## Execution, monitoring and risk management

The most important design principle is that **risk controls must be independent of the agents that seek profit**. The same AI that develops a persuasive trade thesis should not be allowed to decide whether its own leverage, stop or loss limit is acceptable. Financial regulators increasingly expect firms using AI to assess model, data, operational and governance risks and to update their control systems accordingly. NIST’s AI Risk Management Framework likewise emphasises validity, reliability, security, resilience, accountability, transparency and explainability. citeturn5search1turn5search7turn5search8

The risk engine should enforce at least the following control hierarchy:

| Control level | Examples |
|---|---|
| Instrument | Maximum order size, minimum liquidity, spread ceiling, short availability and event restrictions |
| Trade | Maximum loss, compulsory protective stop, approved order types and time-to-live |
| Strategy | Capital allocation, turnover, live drawdown, slippage and regime eligibility |
| Asset class | Forex, crypto and equity exposure limits |
| Correlation cluster | Common USD risk, technology exposure, crypto beta or equity-index beta |
| Portfolio | Gross exposure, net exposure, leverage, value-at-risk, expected shortfall and stress loss |
| Session | Daily loss limit, maximum number of failed orders and operational incident limits |
| Firm | Maximum drawdown, counterparty concentration and total capital-at-risk |
| Infrastructure | Data freshness, clock drift, broker connectivity and reconciliation status |

Illustrative initial limits for a small live deployment could risk approximately 0.10%–0.25% of equity per ordinary trade, impose a daily loss stop near 1%–2%, and keep only a small fraction of total capital in the first canary deployment. These are not universal optimal values. They are conservative starting points that should be derived from strategy volatility, expected loss streaks, liquidity and investor risk tolerance. Raising them should require accumulated live evidence, not a desire to reach a fixed return target.

The system must prohibit unrestricted martingale sizing, doubling after losses and leverage increases whose sole purpose is to recover prior losses. It should also prohibit averaging into a losing position unless this is an explicit component of a validated strategy with a predefined maximum total loss. Margin can magnify losses, and leveraged positions may be liquidated or closed under adverse conditions. citeturn0search13turn0search3

### Trade state machine

Every live position should move through a deterministic state machine:

```text
PROPOSED
   ↓ risk approval
APPROVED
   ↓ order submitted
PENDING
   ↓ fill confirmed
OPEN AND PROTECTED
   ↓ profit threshold met
BREAKEVEN ELIGIBLE
   ↓ trend/volatility condition met
TRAILING
   ↓ target, stop, time limit, risk event or manual halt
EXIT PENDING
   ↓ broker fill reconciled
CLOSED
```

A trade must not be considered open merely because an order was submitted. The broker’s fill record must be received and reconciled. Similarly, a cancellation request does not prove that an order was cancelled. The execution engine must handle partial fills, rejections, duplicate messages, reconnects, stale order status and the possibility that a stop or exit order is active while the local system is offline.

### Stop and profit-protection logic

Protective-stop logic should be deterministic, volatility-aware and broker-aware. A suitable generic model is:

\[
\text{Initial stop distance}
=
\max
\left(
k_{\sigma}\times\text{volatility},
k_s\times\text{spread},
\text{structural invalidation distance}
\right)
\]

The stop should not be so close that ordinary spread movement triggers it, nor so wide that the position exceeds its approved monetary risk. Position size is then derived from the approved risk budget:

\[
\text{Position size}
=
\frac{\text{maximum monetary loss}}
{\text{stop distance}\times\text{instrument value per price unit}}
\]

Profit protection can follow a layered policy:

| Position state | Suggested behaviour |
|---|---|
| Before minimum favourable movement | Maintain the original validated stop |
| At approximately one unit of initial risk in profit | Consider reducing risk or moving to a spread-aware breakeven level |
| Strong trend and favourable liquidity | Trail using volatility, swing structure or a channel |
| Momentum deterioration | Tighten the stop or take partial profit |
| Spread or volatility shock | Reduce position, suspend trailing changes or exit depending on the strategy |
| Broker or data failure | Use broker-native protection and enter safe mode |
| Maximum holding time reached | Exit unless the strategy explicitly permits extension |

Broker-native trailing stops should be used where their exact behaviour is understood. OANDA’s API supports trailing-stop-loss orders linked to open trades, while Interactive Brokers documents trailing stops and trailing-stop limits across its APIs. A trailing stop that becomes a market order can execute at a worse price than the trigger during fast conditions, whereas a trailing-stop-limit order can fail to execute if the market moves through the limit. citeturn1search1turn1search10turn1search0turn1search6

Where the broker supports atomic bracket or one-cancels-other orders, protective orders should be placed with or immediately after entry. Where the broker does not support the desired combination, the local order manager must maintain a redundant synthetic bracket while continuously reconciling broker state. Broker features must be verified per asset and venue rather than assumed to be identical.

### Mandatory circuit breakers

The system should enter safe mode or flatten exposure when any of these conditions occurs:

- Market data exceeds the permitted age or contains sequence gaps.
- Independent feeds differ beyond the allowed tolerance.
- Broker position and internal position do not reconcile.
- Realised slippage exceeds the strategy’s approved distribution.
- Spread, volatility or liquidity crosses emergency thresholds.
- Daily or portfolio drawdown limits are reached.
- An agent attempts to call an unauthorised tool or alter a risk rule.
- Model output changes materially without a corresponding data change.
- Time synchronisation, credential security or network integrity is compromised.
- The system cannot confirm that protective orders are active.

A kill switch should exist at the strategy, asset-class, broker and portfolio levels. No LLM should be able to disable, rewrite or negotiate with the kill switch during a live session.

## Research, backtesting and strategy promotion

The research factory should treat every new strategy as an unproven hypothesis. The primary output of a research agent is not executable code but a complete experimental specification containing the hypothesised edge, decision frequency, universe, features, expected mechanism, cost assumptions and falsification conditions.

Backtest overfitting is one of the largest threats to an automated research operation because an AI system can generate and test thousands of ideas. The more trials performed, the greater the chance that an apparently exceptional strategy is a statistical accident. The Probability of Backtest Overfitting and the Deflated Sharpe Ratio were developed to address selection bias, multiple testing and non-normal returns, while research on the large number of published asset-pricing factors argues for considerably higher statistical hurdles than conventional testing. citeturn0search8turn0search5turn4search4turn4search24

The complete research pipeline should be:

```text
Hypothesis
    ↓
Formal strategy specification
    ↓
Point-in-time dataset construction
    ↓
Unit and property tests
    ↓
In-sample research
    ↓
Walk-forward and purged validation
    ↓
Full transaction-cost simulation
    ↓
Stress, sensitivity and adversarial testing
    ↓
Locked unseen test
    ↓
Paper trading
    ↓
Shadow execution against live quotes
    ↓
Small-capital canary
    ↓
Controlled capital scaling
    ↓
Continuous monitoring or retirement
```

### Data integrity

Backtests must use information that was actually available at the simulated decision time. That includes historical index constituents, delisted equities, corporate actions, publication timestamps, news revisions, economic-data revisions, funding rates, borrow availability and exchange outages. A static list of today’s successful assets introduces survivorship bias. Using a closing price to create a signal and assuming execution at that same closing price can introduce look-ahead bias.

Each dataset should preserve:

| Field | Purpose |
|---|---|
| Event time | When the market event occurred |
| Availability time | When the system could first have received it |
| Processing time | When the system ingested it |
| Source | Exchange, broker, news provider or regulatory filing |
| Revision identifier | Distinguishes first release from later corrections |
| Quality flags | Missing, stale, crossed, outlier or reconstructed |
| Licence and entitlement | Confirms permitted research and live usage |

Recent audits of LLM trading research specifically identify temporal leakage, static universes, missing cost models and unclear execution semantics as reasons not to interpret historical results as deployment evidence. citeturn6view1turn7view1

### Realistic cost and execution simulation

The simulator must deduct:

\[
\text{Net P\&L}
=
\text{Gross P\&L}
-
\text{spread}
-
\text{commission}
-
\text{slippage}
-
\text{market impact}
-
\text{funding}
-
\text{borrow cost}
-
\text{exchange fees}
-
\text{inference and infrastructure cost}
\]

For scalping, bar-based backtesting is generally insufficient. The simulator should model bid and ask prices, message or event order, latency, partial fills, fill probability, queue position where possible, price gaps and adverse selection. Research into cryptocurrency microstructure notes that latency, jitter and queue dynamics are difficult to simulate accurately and can cause live behaviour to diverge substantially from a baseline backtest. citeturn4search30

Cost assumptions should be stressed at multiples of the expected value. A strategy that is profitable only with zero slippage should not be promoted. A robust promotion requirement could demand positive net expectancy at the normal cost model and continued viability under 1.5 or 2 times expected costs.

### Validation methods

The validation desk should use:

| Test | Purpose |
|---|---|
| Walk-forward optimisation | Tests repeated retraining without using future periods |
| Purged and embargoed cross-validation | Reduces leakage from overlapping labels and adjacent observations |
| Locked unseen holdout | Prevents repeated optimisation against the final evaluation period |
| Parameter-surface analysis | Rejects isolated “magic” parameter combinations |
| Deflated Sharpe Ratio | Adjusts apparent performance for multiple trials and non-normality |
| Probability of Backtest Overfitting | Estimates the chance that selected performance is a research artefact |
| Block bootstrap | Preserves some time dependence while testing outcome uncertainty |
| Regime segmentation | Tests trends, crashes, ranges, low liquidity and high volatility separately |
| Cost and latency stress | Measures sensitivity to implementation deterioration |
| Capacity stress | Tests larger order sizes and market impact |
| Agent ablation | Determines whether each agent adds genuine net value |
| Counterfactual prompts | Tests whether LLM decisions respond sensibly to opposing evidence |
| Seed and model variation | Measures sensitivity to model stochasticity and provider choice |

The final unseen period should be opened only once. If researchers inspect it, modify the strategy and test again, it is no longer unseen. Recent walk-forward research in cryptocurrency strategies found that parameter combinations that looked strongest during training could underperform alternatives in unseen data, illustrating why optimisation performance should not be equated with future robustness. citeturn4search18

### Strategy promotion gates

An illustrative promotion framework is:

| Stage | Minimum evidence |
|---|---|
| Research candidate | Economic rationale, reproducible code and complete data lineage |
| Validated backtest | Positive net expectancy, stable parameters and no known leakage |
| Independent replication | Separate validator reproduces results from source data |
| Paper trading | Reliable order logic and no unexplained position mismatches |
| Shadow live | Simulated fills compared with actual live market conditions |
| Canary deployment | Small capital, compulsory stops and strict drawdown limit |
| Limited production | Stable live slippage, expectancy and operational metrics |
| Scaled production | Sufficient live history across more than one regime |
| Retirement | Edge decay, excessive correlation, structural market change or failed controls |

A five-day burst of profits must not qualify a strategy for scaling. For high-frequency strategies, the system needs a sufficiently large number of independent or weakly dependent trades. For swing strategies, it needs longer chronological coverage because hundreds of trades compressed into one market regime are not equivalent to performance across several regimes.

The system should maintain a **trial ledger** recording every attempted strategy, parameter search and model version. Failed trials must not be deleted, because the number of experiments affects statistical confidence. A strategy’s Sharpe ratio is less persuasive if it was the best of ten thousand attempts than if it was the sole pre-registered hypothesis.

## Technology, data and AI architecture

The live system should separate the research plane from the execution plane:

```text
Research plane
LLMs • notebooks • feature research • backtests • document retrieval
             │
             │ signed, versioned strategy package
             ▼
Control plane
model registry • approvals • configuration • risk policies • deployment gates
             │
             ▼
Live trading plane
market data → signals → portfolio → risk → orders → broker → reconciliation
             │
             ▼
Observability plane
logs • metrics • traces • alerts • audit records • P&L attribution
```

The execution plane should remain operational even if all LLM services are unavailable. Existing positions must continue to be monitored, protective orders maintained and risk limits enforced. An outage at an AI provider must never leave unprotected trades.

### Suggested component design

| Layer | Recommended capability |
|---|---|
| Market-data gateway | Normalise exchange and broker feeds into a common event schema |
| Historical store | Immutable raw event storage plus adjusted research datasets |
| Streaming bus | Publish quotes, trades, orders, fills, signals and risk events |
| Feature engine | Compute deterministic technical, microstructure and cross-asset features |
| Research environment | Isolated notebooks and batch workers without live order credentials |
| Agent orchestration | Role-specific agents with limited tool permissions and structured outputs |
| Strategy registry | Versioned code, parameters, validation reports and authorised instruments |
| Portfolio engine | Converts expected returns and risk into target positions |
| Risk engine | Stateless pre-trade checks plus stateful portfolio and drawdown controls |
| Order management | Idempotent order submission, amendment, cancellation and reconciliation |
| Broker adapters | Separate interfaces for forex, crypto and securities brokers |
| Monitoring | Metrics, logs, traces, latency, slippage and data-quality dashboards |
| Audit ledger | Append-only record of decisions, model versions, prompts, orders and fills |
| Secret management | Short-lived credentials, rotation and per-service permissions |
| Disaster recovery | Tested failover, broker-native stops and restart reconciliation |

Broker selection should be driven by regulation, instrument access, API reliability, order types, data quality, costs and availability in the operator’s jurisdiction. Interactive Brokers documents numerous API order types including trailing stops; OANDA provides programmatic forex access and dependent stop orders; Coinbase Advanced Trade provides REST order management and WebSocket market and user-order data. These are examples rather than endorsements, and availability differs by country, account type and instrument. citeturn1search0turn1search7turn1search10turn1search2turn1search5

API keys should have only the permissions needed for trading and data access. Crypto-exchange keys should normally have withdrawal permissions disabled. Research services should not possess live-trading credentials, and production services should not allow arbitrary code execution. Separate subaccounts can limit counterparty and strategy exposure.

### Appropriate use of AI

AI should be concentrated where language, unstructured information and hypothesis generation offer genuine value:

| AI-appropriate task | Deterministic alternative required |
|---|---|
| Extracting structured events from news and filings | Source timestamps and schema validation |
| Generating research hypotheses | Independent backtest and validation |
| Producing bull and bear cases | Numerical evidence and counterfactual testing |
| Identifying possible regime changes | Statistical regime model |
| Writing prototype strategy code | Unit tests, static analysis and code review |
| Explaining post-trade outcomes | Deterministic P&L attribution |
| Detecting unusual operational patterns | Hard thresholds and incident rules |
| Recommending portfolio changes | Independent optimiser and risk engine |

The system should not use an LLM’s verbal confidence—such as “high conviction”—directly as a position-size multiplier. Any probability used in sizing must be calibrated against out-of-sample outcomes, including regime-conditioned reliability curves. Current research warns that language-model confidence can be miscalibrated and that fluency does not imply accurate calculation of P&L, leverage or exposure. citeturn7view0

Agent memory should store structured observations and outcomes, not merely free-form reflections. For each decision, it should capture what was known, which features were active, what the agent predicted, the resulting action, realised return, cost, maximum adverse excursion, maximum favourable excursion and whether the thesis was invalidated. This enables conditional scoring such as “useful during liquid trending forex sessions but unreliable during news shocks”.

## Governance, regulation and implementation roadmap

The legal position depends on where the entity is established, where the broker or exchange is regulated, whether it trades only proprietary capital, whether it manages client money and which products it uses. Operating a private proprietary system is generally different from offering investment advice, managing third-party assets, operating a fund, soliciting deposits or executing trades for clients.

For an Eswatini-based operation, the Financial Services Regulatory Authority supervises non-bank financial services, including capital-markets activities. The latest official Central Bank of Eswatini material located for this research stated that cryptocurrencies were not legal tender and that, at the time of that publication, there was no dedicated cryptocurrency legislation or regulation in Eswatini; it warned that participants traded at their own risk. This status must be confirmed directly with the Central Bank and FSRA before launch because regulatory frameworks can change. citeturn8search2turn8search4turn8search1

The jurisdiction of the broker also matters. In the United States, FINRA’s new intraday margin standards took effect on 4 June 2026 and replaced the former pattern-day-trader trade-count and US$25,000 minimum-equity framework. In the United Kingdom, retail access to certain crypto exchange-traded notes was opened in 2025, while the FCA stated that the retail crypto-derivatives ban remained in place and continued to be reviewed. These examples show why instrument- and account-specific compliance cannot be hard-coded from old internet guidance. citeturn3search0turn3search24turn3search11turn3search3

If the operation will manage other people’s capital, provide signals, market automated-trading services or receive performance fees, specialist legal advice is required before accepting funds. The system may need licences, client-money controls, disclosures, suitability processes, anti-money-laundering controls, custody arrangements and audited reporting.

### Recommended implementation programme

| Phase | Deliverables | Capital status |
|---|---|---|
| Foundation | Data contracts, event schemas, historical store, audit ledger and broker sandbox | No live capital |
| Research laboratory | Initial momentum, mean-reversion, breakout and pairs strategies | No live capital |
| Validation framework | Point-in-time backtester, cost model, PBO/DSR, stress and trial ledger | No live capital |
| Multi-agent research team | Analyst, challenger, researcher and validation agents | No live capital |
| Execution simulator | Order state machine, partial fills, stops and reconciliation | No live capital |
| Paper-trading house | Full workflow on live data with simulated orders | Paper only |
| Shadow trading | Compare theoretical orders with obtainable market prices | Paper only |
| Canary production | One broker, few instruments, one or two strategies | Approximately 1%–5% of intended capital |
| Controlled expansion | Additional strategies, asset classes and brokers | Scale only from live evidence |
| Mature operation | Continuous strategy research, independent risk and full incident management | Risk-budgeted production |

The initial live universe should be deliberately small: a few highly liquid instruments, one broker per asset class and a limited number of strategies. Starting simultaneously with many crypto pairs, equity universes and forex crosses makes failures difficult to diagnose and increases the chance of hidden correlated exposure.

### Production scorecard

Return alone is insufficient. The operating dashboard should track:

| Category | Metrics |
|---|---|
| Performance | Net return, expectancy, profit factor, Sharpe, Sortino and drawdown |
| Trade quality | Win rate, payoff ratio, adverse and favourable excursion |
| Execution | Spread paid, slippage, fill rate, reject rate and latency |
| Risk | Gross and net exposure, leverage, stress loss and concentration |
| Strategy health | Live-versus-backtest divergence, feature drift and regime fit |
| Agent value | Forecast calibration, disagreement, incremental P&L and research hit rate |
| Operations | Feed gaps, broker outages, reconciliation breaks and recovery time |
| AI cost | Token use, inference latency, provider errors and cost per accepted signal |
| Compliance | Blocked orders, audit completeness and rule exceptions |

The return target should be reframed as a **stretch outcome**, not a commitment. The trading house can pursue asymmetric opportunities, use controlled leverage and trade actively, but it should not add features designed to force a doubling schedule. Features that appear to help meet such a schedule—extreme leverage, martingale sizing, removal of stops, concentration in a single volatile instrument or loss-chasing—would increase the probability of ruin rather than create a dependable edge.

The strongest achievable system is therefore one that:

1. Lets AI research continuously but never promotes a strategy without independent evidence.
2. Uses multiple strategy families rather than relying on one scalping method.
3. Keeps LLMs outside the latency-critical and safety-critical execution path.
4. Calculates position size from predefined loss limits rather than desired profit.
5. Protects every live trade with broker-native or redundant stop logic.
6. Scores agents and strategies on net, out-of-sample and live performance.
7. Assumes extraordinary backtest returns are errors until proven otherwise.
8. Scales capital slowly enough to detect regime, capacity and implementation failure.
9. Maintains a non-negotiable risk veto and kill switch.
10. Optimises for long-term survival and compounding rather than a five-day doubling promise.

This architecture can create a sophisticated, continuously researching and largely autonomous trading operation. It cannot ensure 100% profit every five working days, and no credible trading technology can make that guarantee. Its defensible advantage would come from disciplined research throughput, diverse strategies, faster information processing, precise execution, rigorous cost modelling and stronger risk control—not from attempting to compel the market to satisfy a predetermined return schedule.