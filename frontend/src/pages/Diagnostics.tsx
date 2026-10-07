import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, useInvalidate } from "../api";
import { TaskList } from "../components/Tasks";
import { Button, ErrorNotice } from "../components/common";

// Development probes (SAMPLE_ONLY); not part of normal use.
export function DiagnosticsPage() {
  const [ticker, setTicker] = useState("AAPL");
  const invalidate = useInvalidate();
  const mutation = useMutation({
    mutationFn: (kind: string) =>
      api("/diagnostics/collections", "POST", {
        kind,
        ...(kind === "market_probe" ? { ticker } : {}),
      }),
    onSuccess: invalidate,
  });
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>开发诊断</h1>
          <p>SAMPLE_ONLY · 来源探测不会成为正式覆盖或研究结果。</p>
        </div>
        <Link className="text-link" to="/data">
          返回数据与任务
        </Link>
      </div>
      <section className="panel">
        <div className="analysis-form">
          <label>
            诊断股票
            <input
              value={ticker}
              onChange={(e) => setTicker(e.target.value.toUpperCase())}
            />
          </label>
          <Button
            busy={mutation.isPending}
            onClick={() => mutation.mutate("market_probe")}
          >
            行情小样验证
          </Button>
          <Button
            busy={mutation.isPending}
            onClick={() => mutation.mutate("sec_probe")}
          >
            SEC 来源验证
          </Button>
          <Button
            busy={mutation.isPending}
            onClick={() => mutation.mutate("fixture_check")}
          >
            本地合成任务
          </Button>
        </div>
        <ErrorNotice error={mutation.error} />
        <TaskList />
      </section>
    </>
  );
}
