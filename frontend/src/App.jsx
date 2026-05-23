import React from "react";
import { useWallet, useStaking } from "./hooks/useStaking";
import StatsPanel from "./components/StatsPanel";
import StakingDashboard from "./components/StakingDashboard";
import { shortenAddress } from "./utils/contracts";

export default function App() {
  const { account, signer, chainId, isCorrectChain, loading: walletLoading, connect, switchToBSC } = useWallet();
  const { data, stats, loading, txLoading, approve, stake, requestUnlock, unstake, claimRewards } = useStaking(signer, account);

  return (
    <div className="min-h-screen" style={{ background: "radial-gradient(ellipse at 50% 0%, #1a1200 0%, #080a0f 60%)" }}>

      {/* ── Header ── */}
      <header style={{ background: "rgba(8,10,15,0.92)", borderBottom: "1px solid rgba(245,200,66,0.12)" }}
              className="backdrop-blur-sm sticky top-0 z-50">
        <div className="max-w-6xl mx-auto px-4 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl flex items-center justify-center font-bold text-sm"
                 style={{ background: "linear-gradient(135deg, #f5c842, #f0a500)", color: "#080a0f" }}>S</div>
            <span className="font-bold text-lg gradient-text tracking-wide">StakingPlatform</span>
          </div>
          <div className="flex items-center gap-3">
            {account && (
              <div className="flex items-center gap-2 text-sm">
                <div className={`w-2 h-2 rounded-full ${isCorrectChain ? "bg-green-400" : "bg-red-400"} pulse`} />
                <span className="text-slate-400 hidden md:block">
                  {isCorrectChain ? "BSC Mainnet" : "Wrong network"}
                </span>
              </div>
            )}
            {account ? (
              <div className="card px-4 py-2 text-sm font-mono" style={{ color: "#f5c842" }}>
                {shortenAddress(account)}
              </div>
            ) : (
              <button className="btn-primary" style={{ width: "auto", padding: "10px 22px" }}
                onClick={connect} disabled={walletLoading}>
                {walletLoading ? "⏳ Connecting..." : "🔗 Connect Wallet"}
              </button>
            )}
          </div>
        </div>
      </header>

      {/* ── Hero ── */}
      <div className="max-w-6xl mx-auto px-4 pt-16 pb-10 text-center">
        <div className="inline-flex items-center gap-2 px-4 py-1.5 rounded-full text-xs font-semibold mb-6"
             style={{ background: "rgba(245,200,66,0.08)", border: "1px solid rgba(245,200,66,0.25)", color: "#f5c842" }}>
          🔥 APY 50% first month · 10% after · 7 day unlock
        </div>
        <h1 className="text-5xl md:text-6xl font-extrabold mb-4 tracking-tight">
          <span className="gradient-text">Stake & Earn</span>
        </h1>
        <p className="text-slate-400 text-lg max-w-lg mx-auto leading-relaxed">
          Put your tokens to work. Earn rewards automatically, 24/7, directly on BSC.
        </p>

        {/* Decorative line */}
        <div className="mt-10 h-px max-w-xs mx-auto" 
             style={{ background: "linear-gradient(90deg, transparent, rgba(245,200,66,0.4), transparent)" }} />
      </div>

      {/* ── Main ── */}
      <main className="max-w-6xl mx-auto px-4 pb-20">
        <StatsPanel stats={stats} />

        {!account ? (
          <div className="card glow p-14 text-center">
            <div className="text-6xl mb-5">🔐</div>
            <h2 className="text-2xl font-bold mb-3">Connect Your Wallet</h2>
            <p className="text-slate-400 mb-8 max-w-sm mx-auto">Connect MetaMask to stake tokens and view your rewards.</p>
            <button className="btn-primary mx-auto" style={{ maxWidth: 280 }} onClick={connect} disabled={walletLoading}>
              {walletLoading ? "⏳ Connecting..." : "🦊 Connect MetaMask"}
            </button>
          </div>
        ) : !isCorrectChain ? (
          <div className="card p-14 text-center">
            <div className="text-6xl mb-5">⚠️</div>
            <h2 className="text-2xl font-bold mb-3">Switch Network</h2>
            <p className="text-slate-400 mb-8">You need to be connected to <strong>BSC Mainnet</strong>.</p>
            <button className="btn-primary mx-auto" style={{ maxWidth: 280 }} onClick={switchToBSC}>
              🔄 Switch to BSC Mainnet
            </button>
          </div>
        ) : loading && !data ? (
          <div className="card p-14 text-center">
            <div className="w-12 h-12 border-4 border-t-transparent rounded-full animate-spin mx-auto mb-4"
                 style={{ borderColor: "rgba(245,200,66,0.3)", borderTopColor: "transparent" }} />
            <p className="text-slate-400">Loading blockchain data...</p>
          </div>
        ) : (
          <StakingDashboard data={data} txLoading={txLoading} approve={approve} stake={stake}
            requestUnlock={requestUnlock} unstake={unstake} claimRewards={claimRewards} account={account} />
        )}
      </main>

      {/* ── Footer ── */}
      <footer style={{ borderTop: "1px solid rgba(245,200,66,0.08)" }}
              className="py-6 text-center text-slate-600 text-sm">
        Built on BNB Smart Chain · Smart contract verified on BscScan
      </footer>
    </div>
  );
}
