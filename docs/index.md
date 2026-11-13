---
template: home.html
hide:
  - navigation
  - toc
---

# even-simpler-salesforce

<section class="esf-why" markdown="0">
  <p class="esf-why__eyebrow">Beyond simple-salesforce</p>
  <h2 class="esf-why__title">The parts you no longer write yourself</h2>
  <p class="esf-why__lede">Six things simple-salesforce leaves to you. Here they come built in.</p>
  <div class="esf-why__grid">
    <a class="esf-card" href="getting-started/#async-or-sync">
      <span class="esf-card__title">Async and sync</span>
      <span class="esf-card__text">One API. Await it in FastAPI, call it from Flask or a script.</span>
      <pre class="esf-window__code esf-card__code"><code><span class="k">async</span> <span class="k">with</span> <span class="n">esf</span><span class="o">.</span><span class="n">AsyncSalesforce</span><span class="p">(</span><span class="n">auth</span><span class="p">)</span> <span class="k">as</span> <span class="n">sf</span><span class="p">:</span>
    <span class="k">await</span> <span class="n">sf</span><span class="o">.</span><span class="n">query</span><span class="p">(</span><span class="n">soql</span><span class="p">)</span>

<span class="k">with</span> <span class="n">esf</span><span class="o">.</span><span class="n">Salesforce</span><span class="p">(</span><span class="n">auth</span><span class="p">)</span> <span class="k">as</span> <span class="n">sf</span><span class="p">:</span>
    <span class="n">sf</span><span class="o">.</span><span class="n">query</span><span class="p">(</span><span class="n">soql</span><span class="p">)</span></code></pre>
      <span class="esf-card__vs"><span>simple-salesforce</span> Sync only</span>
    </a>
    <a class="esf-card" href="authentication/#how-tokens-are-handled">
      <span class="esf-card__title">Token refresh</span>
      <span class="esf-card__text">An expired token is refreshed once, and the call is sent again.</span>
      <pre class="esf-window__code esf-card__code"><code><span class="c1"># The token expired an hour ago.</span>
<span class="k">await</span> <span class="n">sf</span><span class="o">.</span><span class="n">query</span><span class="p">(</span><span class="s2">&quot;SELECT Id FROM Lead&quot;</span><span class="p">)</span>
<span class="c1"># 401 -&gt; refreshed -&gt; sent again -&gt; rows</span></code></pre>
      <span class="esf-card__vs"><span>simple-salesforce</span> Raises SalesforceExpiredSession</span>
    </a>
    <a class="esf-card" href="errors/">
      <span class="esf-card__title">Typed errors</span>
      <span class="esf-card__text">One exception class per Salesforce error code, with the fields that failed.</span>
      <pre class="esf-window__code esf-card__code"><code><span class="k">except</span> <span class="n">esf</span><span class="o">.</span><span class="n">ValidationRuleFailed</span> <span class="k">as</span> <span class="n">exc</span><span class="p">:</span>
    <span class="nb">print</span><span class="p">(</span><span class="n">exc</span><span class="o">.</span><span class="n">fields</span><span class="p">)</span>  <span class="c1"># (&#39;Company&#39;,)</span>
<span class="k">except</span> <span class="n">esf</span><span class="o">.</span><span class="n">RowLocked</span><span class="p">:</span>
    <span class="n">retry_later</span><span class="p">()</span></code></pre>
      <span class="esf-card__vs"><span>simple-salesforce</span> One class per HTTP status</span>
    </a>
    <a class="esf-card" href="usage-and-budgets/#budgets">
      <span class="esf-card__title">API budget</span>
      <span class="esf-card__text">Every app on the org shares one daily API limit. Pause batch jobs first, so live traffic keeps working.</span>
      <pre class="esf-window__code esf-card__code"><code><span class="n">budget</span> <span class="o">=</span> <span class="n">esf</span><span class="o">.</span><span class="n">Budget</span><span class="p">()</span>
<span class="n">sf</span> <span class="o">=</span> <span class="n">esf</span><span class="o">.</span><span class="n">AsyncSalesforce</span><span class="p">(</span><span class="n">auth</span><span class="p">,</span> <span class="n">budget</span><span class="o">=</span><span class="n">budget</span><span class="p">)</span>
<span class="n">job</span> <span class="o">=</span> <span class="n">sf</span><span class="o">.</span><span class="n">tagged</span><span class="p">(</span><span class="n">priority</span><span class="o">=</span><span class="s2">&quot;low&quot;</span><span class="p">)</span>
<span class="c1"># above 80% of the daily limit, job calls</span>
<span class="c1"># raise BudgetExceeded. Live calls still run.</span></code></pre>
      <span class="esf-card__vs"><span>simple-salesforce</span> Reads usage, never stops a call</span>
    </a>
    <a class="esf-card" href="logging/">
      <span class="esf-card__title">Readable logs</span>
      <span class="esf-card__text">One event per call, secrets redacted. Works with structlog and OpenTelemetry.</span>
      <pre class="esf-window__code esf-card__code"><code><span class="c1">$</span> ESF_DEBUG=1 python sync.py
esf GET   query          Lead 200 115ms
esf PATCH sobject.update Lead 400  88ms
esf POST  oauth.refresh  rotated  240ms</code></pre>
      <span class="esf-card__vs"><span>simple-salesforce</span> No structured events</span>
    </a>
    <a class="esf-card" href="testing/">
      <span class="esf-card__title">Test fakes</span>
      <span class="esf-card__text">An in-memory org as a pytest fixture. No network, no mocks to write.</span>
      <pre class="esf-window__code esf-card__code"><code><span class="k">def</span><span class="w"> </span><span class="nf">test_close</span><span class="p">(</span><span class="n">esf_fake</span><span class="p">):</span>
    <span class="n">lead</span> <span class="o">=</span> <span class="n">esf_fake</span><span class="o">.</span><span class="n">add</span><span class="p">(</span><span class="s2">&quot;Lead&quot;</span><span class="p">,</span> <span class="n">record</span><span class="p">)</span>
    <span class="n">close_lead</span><span class="p">(</span><span class="n">esf_fake</span><span class="p">,</span> <span class="n">lead</span><span class="p">)</span>
    <span class="k">assert</span> <span class="n">esf_fake</span><span class="o">.</span><span class="n">calls_for</span><span class="p">(</span><span class="s2">&quot;sobject.update&quot;</span><span class="p">)</span></code></pre>
      <span class="esf-card__vs"><span>simple-salesforce</span> No test helpers</span>
    </a>
  </div>
  <a class="esf-migrate" href="migrate/">
    <span class="esf-migrate__text"><strong>Moving over?</strong> One command rewrites the mechanical part of your code.</span>
    <code class="esf-migrate__cmd">esf migrate src/</code>
  </a>
</section>

!!! note "Beta (0.5)"
    Breaking changes are still possible in 0.x releases. See the
    [project status](getting-started.md#project-status) for what is covered and where it has been tested.
