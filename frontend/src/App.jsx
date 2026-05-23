import React from "react";
import { useWallet, useStaking } from "./hooks/useStaking";
import StatsPanel from "./components/StatsPanel";
import StakingDashboard from "./components/StakingDashboard";
import { shortenAddress } from "./utils/contracts";

export default function App() {
  const { account, signer, chainId, isCorrectChain, loading: walletLoading, connect, switchToBSC } = useWallet();
  const { data, stats, loading, txLoading, approve, stake, requestUnlock, unstake, claimRewards } = useStaking(signer, account);

  return (
    <div className="min-h-screen" style={{ background: "radial-gradient(ellipse at top, #1a1040 0%, #0d0f1e 60%)" }}>
      <header className="border-b border-indigo-900/30 backdrop-blur-sm sticky top-0 z-50"
              style={{ background: "rgba(13,15,30,0.85)" }}>
        <div className="max-w-6xl mx-auto px-4 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center font-bold text-sm">S</div>
            <span className="font-bold text-lg gradient-text">StakingPlatform</span>
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
              <div className="card px-4 py-2 text-sm font-mono text-indigo-300">
                {shortenAddress(account)}
              </div>
            ) : (
              <button
                className="btn-primary"
                style={{ width: "auto", padding: "10px 20px" }}
                onClick={connect}
                disabled={walletLoading}
              >
                {walletLoading ? "⏳ Connecting..." : "🔗 Connect Wallet"}
              </button>
            )}
          </div>
        </div>
      </header>

      <div className="max-w-6xl mx-auto px-4 pt-12 pb-8 text-center">
        <div className="inline-block bg-indigo-900/40 border border-indigo-700/40 text-indigo-300 text-xs px-3 py-1 rounded-full mb-4">
          🔥 APY 50% first month · 10% after · 7 day unlock
        </div>
        <h1 className="text-4xl md:text-5xl font-extrabold mb-3">
          <span className="gradient-text">Stake & Earn</span>
        </h1>
        <p className="text-slate-400 text-lg max-w-xl mx-auto">
          Put your tokens to work. Earn rewards automatically, 24/7, directly on BSC.
        </p>
      </div>

      <main className="max-w-6xl mx-auto px-4 pb-16">
        <StatsPanel stats={stats} />
        {!account ? (
          <div className="card p-12 text-center">
            <div className="text-6xl mb-4">🔐</div>
            <h2 className="text-2xl font-bold mb-2">Connect Your Wallet</h2>
            <p className="text-slate-400 mb-6">Connect MetaMask to stake tokens and view your rewards.</p>
            <button className="btn-primary mx-auto" style={{ maxWidth: 280 }} onClick={connect} disabled={walletLoading}>
              {walletLoading ? "⏳ Connecting..." : "🦊 Connect MetaMask"}
            </button>
          </div>
        ) : !isCorrectChain ? (
          <div className="card p-12 text-center">
            <div className="text-6xl mb-4">⚠️</div>
            <h2 className="text-2xl font-bold mb-2">Switch Network</h2>
            <p className="text-slate-400 mb-6">You need to be connected to <strong>BSC Mainnet</strong>.</p>
            <button className="btn-primary mx-auto" style={{ maxWidth: 280 }} onClick={switchToBSC}>
              🔄 Switch to BSC Mainnet
            </button>
          </div>
        ) : loading && !data ? (
          <div className="card p-12 text-center">
            <div className="w-12 h-12 border-4 border-indigo-500 border-t-transparent rounded-full animate-spin mx-auto mb-4" />
            <p className="text-slate-400">Loading blockchain data...</p>
          </div>
        ) : (
          <StakingDashboard data={data} txLoading={txLoading} approve={approve} stake={stake}
            requestUnlock={requestUnlock} unstake={unstake} claimRewards={claimRewards} account={account} />
        )}
      </main>

      <footer className="border-t border-indigo-900/20 py-6 text-center text-slate-600 text-sm">
        Built on BNB Smart Chain · Smart contract verified on BscScan
      </footer>
    </div>
  );
}