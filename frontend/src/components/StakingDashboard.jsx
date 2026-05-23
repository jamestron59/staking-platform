import React, { useState } from "react";
import { ethers } from "ethers";
import { formatAmount, secondsToTime, shortenAddress, TOKEN_ADDRESS, STAKING_ADDRESS } from "../utils/contracts";

export default function StakingDashboard({ data, txLoading, approve, stake, requestUnlock, unstake, claimRewards }) {
  const [stakeAmount, setStakeAmount] = useState("");

  if (!data) return (
    <div className="card p-8 text-center">
      <div className="w-12 h-12 border-4 border-t-transparent rounded-full animate-spin mx-auto mb-4"
           style={{ borderColor: "rgba(245,200,66,0.3)", borderTopColor: "transparent" }} />
      <p className="text-slate-400">Loading data...</p>
    </div>
  );

  const { stakedAmount, pendingRewards, unlockTimeLeft, canUnstake,
          apy, tokenBalance, allowance, decimals, unlockRequested, stakedAt } = data;

  const stakeAmountBig = stakeAmount
    ? (() => { try { return ethers.parseUnits(stakeAmount, decimals); } catch { return 0n; } })()
    : 0n;

  const needsApproval = stakeAmountBig > 0n && allowance < stakeAmountBig;
  const hasBalance    = tokenBalance > 0n;
  const hasStake      = stakedAmount > 0n;
  const hasRewards    = pendingRewards > 0n;

  const setMax = () => {
    try { setStakeAmount(ethers.formatUnits(tokenBalance, decimals)); } catch {}
  };

  const handleStake = async () => {
    if (!stakeAmountBig || stakeAmountBig === 0n) return;
    if (needsApproval) { await approve(stakeAmountBig); }
    else { await stake(stakeAmountBig); setStakeAmount(""); }
  };

  const gold = "#f5c842";
  const goldBg = "rgba(245,200,66,0.08)";
  const goldBorder = "rgba(245,200,66,0.2)";

  return (
    <div className="grid md:grid-cols-2 gap-6">

      {/* ── Stake Panel ── */}
      <div className="card p-6">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-xl">🚀</span>
          <h2 className="text-lg font-bold">Stake Tokens</h2>
        </div>
        <p className="text-slate-400 text-sm mb-6">
          Current APY: <span className="font-bold" style={{ color: gold }}>{apy}%</span>
          {apy >= 50 && <span className="ml-2 text-xs px-2 py-0.5 rounded-full font-semibold"
            style={{ background: "rgba(245,200,66,0.12)", color: gold }}>🔥 Bonus Active</span>}
        </p>

        <div className="mb-5">
          <div className="flex justify-between text-sm mb-2">
            <span className="text-slate-400">Available balance</span>
            <span style={{ color: gold }}>{formatAmount(tokenBalance, decimals, 4)} tokens</span>
          </div>
          <div className="relative">
            <input type="number" className="input-field pr-16" placeholder="0.00"
              value={stakeAmount} onChange={e => setStakeAmount(e.target.value)} min="0" />
            <button onClick={setMax}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-sm font-bold"
              style={{ color: gold }}>MAX</button>
          </div>
        </div>

        <button className="btn-primary" onClick={handleStake}
          disabled={!stakeAmountBig || stakeAmountBig === 0n || txLoading || !hasBalance}>
          {txLoading ? "⏳ Processing..." : needsApproval ? "🔓 Approve Tokens" : "🚀 Stake Now"}
        </button>

        {hasStake && (
          <div className="mt-5 p-4 rounded-xl" style={{ background: goldBg, border: `1px solid ${goldBorder}` }}>
            <p className="text-slate-400 text-xs uppercase tracking-wider mb-1">Your Staked Amount</p>
            <p className="text-2xl font-bold" style={{ color: gold }}>{formatAmount(stakedAmount, decimals, 4)}</p>
            <p className="text-slate-500 text-xs mt-1">tokens</p>
            {stakedAt > 0 && (
              <p className="text-slate-500 text-xs mt-1">
                Since {new Date(stakedAt * 1000).toLocaleDateString("en-US")}
              </p>
            )}
          </div>
        )}
      </div>

      {/* ── Rewards Panel ── */}
      <div className="card p-6">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-xl">🏆</span>
          <h2 className="text-lg font-bold">Rewards</h2>
        </div>
        <p className="text-slate-400 text-sm mb-6">Accumulating in real time</p>

        <div className="p-5 rounded-xl mb-5 text-center"
             style={{ background: "linear-gradient(135deg, rgba(245,200,66,0.06), rgba(240,165,0,0.1))", border: `1px solid ${goldBorder}` }}>
          <p className="text-slate-400 text-sm mb-2">Pending Rewards</p>
          <p className="text-4xl font-extrabold" style={{ color: gold }}>
            {formatAmount(pendingRewards, decimals, 6)}
          </p>
          <p className="text-slate-500 text-sm mt-1">tokens</p>
        </div>

        <button className="btn-primary mb-3" onClick={claimRewards} disabled={!hasRewards || txLoading}>
          {txLoading ? "⏳ Processing..." : "🏆 Claim Rewards"}
        </button>

        <p className="text-xs text-slate-500 text-center">Claim anytime, no penalty</p>
      </div>

      {/* ── Unstake Panel ── */}
      <div className="card p-6 md:col-span-2">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-xl">🔓</span>
          <h2 className="text-lg font-bold">Withdraw Tokens</h2>
        </div>
        <p className="text-slate-400 text-sm mb-6">
          Unlock period: <span className="font-semibold" style={{ color: gold }}>7 days</span>
        </p>

        <div className="flex flex-col md:flex-row gap-4">
          <div className="flex-1 p-5 rounded-xl" style={{
            background: unlockRequested ? "rgba(245,200,66,0.05)" : "rgba(255,255,255,0.02)",
            border: `1px solid ${unlockRequested ? "rgba(245,200,66,0.3)" : "rgba(255,255,255,0.06)"}` }}>
            <div className="flex items-center gap-2 mb-3">
              <div className="w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold"
                   style={{ background: unlockRequested ? "linear-gradient(135deg,#f5c842,#f0a500)" : "rgba(255,255,255,0.1)", color: unlockRequested ? "#080a0f" : "#fff" }}>1</div>
              <span className="font-semibold">Request Unlock</span>
              {unlockRequested && <span className="text-xs px-2 py-0.5 rounded-full" style={{ background: "rgba(245,200,66,0.15)", color: gold }}>✓ Done</span>}
            </div>
            <p className="text-slate-400 text-xs mb-4">Starts the 7-day countdown timer</p>
            <button className="btn-secondary text-sm py-2" onClick={requestUnlock}
              disabled={!hasStake || unlockRequested || txLoading}>
              {unlockRequested ? "✅ Already requested" : "🔓 Request Unlock"}
            </button>
          </div>

          <div className="hidden md:flex items-center" style={{ color: gold }}>
            <span className="text-2xl">→</span>
          </div>

          <div className="flex-1 p-5 rounded-xl" style={{
            background: canUnstake ? "rgba(245,200,66,0.05)" : "rgba(255,255,255,0.02)",
            border: `1px solid ${canUnstake ? "rgba(245,200,66,0.3)" : "rgba(255,255,255,0.06)"}` }}>
            <div className="flex items-center gap-2 mb-3">
              <div className="w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold"
                   style={{ background: canUnstake ? "linear-gradient(135deg,#f5c842,#f0a500)" : "rgba(255,255,255,0.1)", color: canUnstake ? "#080a0f" : "#fff" }}>2</div>
              <span className="font-semibold">Withdraw Tokens</span>
            </div>
            {unlockRequested && !canUnstake && (
              <p className="text-xs mb-3" style={{ color: gold }}>⏳ Available in: {secondsToTime(unlockTimeLeft)}</p>
            )}
            {!unlockRequested && <p className="text-slate-400 text-xs mb-4">Available 7 days after request</p>}
            {canUnstake && <p className="text-xs mb-4" style={{ color: gold }}>✅ Ready to withdraw!</p>}
            <button className="btn-primary text-sm py-2" onClick={unstake} disabled={!canUnstake || txLoading}>
              {txLoading ? "⏳ Processing..." : "💰 Withdraw Tokens"}
            </button>
          </div>
        </div>

        <div className="mt-4 p-3 rounded-lg text-xs" style={{ background: "rgba(239,68,68,0.05)", border: "1px solid rgba(239,68,68,0.15)", color: "#f87171" }}>
          ⚠️ <strong>Emergency Withdraw</strong>: Withdraw immediately without rewards.
          <span className="ml-1" style={{ color: "#64748b" }}>(Available directly on BscScan)</span>
        </div>
      </div>

      {/* ── Info Bar ── */}
      <div className="card p-5 md:col-span-2">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-center text-xs">
          {[
            { label: "Staking Contract", value: shortenAddress(STAKING_ADDRESS), href: `https://bscscan.com/address/${STAKING_ADDRESS}` },
            { label: "Token Contract",   value: shortenAddress(TOKEN_ADDRESS),   href: `https://bscscan.com/address/${TOKEN_ADDRESS}` },
            { label: "APY Month 1",      value: "50%",  href: null },
            { label: "APY Standard",     value: "10%",  href: null },
          ].map(item => (
            <div key={item.label}>
              <p className="text-slate-500 mb-1">{item.label}</p>
              {item.href
                ? <a href={item.href} target="_blank" rel="noreferrer"
                     className="font-mono hover:underline" style={{ color: gold }}>{item.value}</a>
                : <p className="font-bold" style={{ color: gold }}>{item.value}</p>}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
