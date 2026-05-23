import React, { useState } from "react";
import { ethers } from "ethers";
import { formatAmount, secondsToTime, shortenAddress, TOKEN_ADDRESS, STAKING_ADDRESS } from "../utils/contracts";

export default function StakingDashboard({ data, txLoading, approve, stake, requestUnlock, unstake, claimRewards, account }) {
  const [stakeAmount, setStakeAmount] = useState("");

  if (!data) return (
    <div className="card p-8 text-center">
      <div className="w-12 h-12 border-4 border-indigo-500 border-t-transparent rounded-full animate-spin mx-auto mb-4" />
      <p className="text-slate-400">Se încarcă datele...</p>
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
    try {
      setStakeAmount(ethers.formatUnits(tokenBalance, decimals));
    } catch {}
  };

  const handleStake = async () => {
    if (!stakeAmountBig || stakeAmountBig === 0n) return;
    if (needsApproval) {
      await approve(stakeAmountBig);
    } else {
      await stake(stakeAmountBig);
      setStakeAmount("");
    }
  };

  return (
    <div className="grid md:grid-cols-2 gap-6">

      {/* ── Panou Stake ─────────────────────────────────────────────── */}
      <div className="card p-6">
        <h2 className="text-lg font-bold mb-1">Stakează Tokens</h2>
        <p className="text-slate-400 text-sm mb-5">
          APY curent: <span className="text-green-400 font-bold">{apy}%</span>
          {apy >= 50 && <span className="ml-2 text-xs bg-green-900 text-green-300 px-2 py-0.5 rounded-full">🔥 Bonus</span>}
        </p>

        <div className="mb-4">
          <div className="flex justify-between text-sm mb-1">
            <span className="text-slate-400">Sold disponibil</span>
            <span className="text-indigo-300">{formatAmount(tokenBalance, decimals, 4)} tokens</span>
          </div>
          <div className="relative">
            <input
              type="number"
              className="input-field pr-16"
              placeholder="0.00"
              value={stakeAmount}
              onChange={e => setStakeAmount(e.target.value)}
              min="0"
            />
            <button
              onClick={setMax}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-indigo-400 text-sm font-bold hover:text-indigo-200"
            >MAX</button>
          </div>
        </div>

        <button
          className="btn-primary"
          onClick={handleStake}
          disabled={!stakeAmountBig || stakeAmountBig === 0n || txLoading || !hasBalance}
        >
          {txLoading ? "⏳ Procesare..." : needsApproval ? "🔓 Aprobare" : "🚀 Stakează"}
        </button>

        {hasStake && (
          <div className="mt-4 p-3 rounded-xl bg-indigo-950/50 border border-indigo-900">
            <p className="text-slate-400 text-xs">Tokens staked</p>
            <p className="text-white font-bold text-xl">{formatAmount(stakedAmount, decimals, 4)}</p>
            {stakedAt > 0 && (
              <p className="text-slate-500 text-xs mt-1">
                Staked pe {new Date(stakedAt * 1000).toLocaleDateString("ro-RO")}
              </p>
            )}
          </div>
        )}
      </div>

      {/* ── Panou Recompense ─────────────────────────────────────────── */}
      <div className="card p-6">
        <h2 className="text-lg font-bold mb-1">Recompense</h2>
        <p className="text-slate-400 text-sm mb-5">Se acumulează în timp real</p>

        <div className="p-4 rounded-xl bg-gradient-to-br from-indigo-950 to-purple-950 border border-indigo-800 mb-5">
          <p className="text-slate-400 text-sm">Recompense în așteptare</p>
          <p className="text-3xl font-bold text-yellow-400 mt-1">
            {formatAmount(pendingRewards, decimals, 6)}
          </p>
          <p className="text-slate-500 text-xs mt-1">tokens</p>
        </div>

        <button
          className="btn-primary mb-3"
          onClick={claimRewards}
          disabled={!hasRewards || txLoading}
        >
          {txLoading ? "⏳ Procesare..." : "🏆 Colectează Recompense"}
        </button>

        <div className="text-xs text-slate-500 text-center">
          Recompensele se pot colecta oricând, fără penalizare
        </div>
      </div>

      {/* ── Panou Unstake ────────────────────────────────────────────── */}
      <div className="card p-6 md:col-span-2">
        <h2 className="text-lg font-bold mb-1">Retragere Tokens</h2>
        <p className="text-slate-400 text-sm mb-5">
          Perioada de unlock: <span className="text-indigo-300 font-semibold">7 zile</span>
        </p>

        <div className="flex flex-col md:flex-row gap-4">
          {/* Step 1 */}
          <div className={`flex-1 p-4 rounded-xl border ${unlockRequested ? "border-green-700 bg-green-950/30" : "border-slate-700 bg-slate-900/30"}`}>
            <div className="flex items-center gap-2 mb-2">
              <div className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold ${unlockRequested ? "bg-green-600" : "bg-indigo-600"}`}>1</div>
              <span className="font-semibold text-sm">Cere Unlock</span>
              {unlockRequested && <span className="text-green-400 text-xs">✓ Cerut</span>}
            </div>
            <p className="text-slate-400 text-xs mb-3">Pornește countdown-ul de 7 zile</p>
            <button
              className="btn-secondary text-sm py-2"
              onClick={requestUnlock}
              disabled={!hasStake || unlockRequested || txLoading}
            >
              {unlockRequested ? "✅ Cerut deja" : "🔓 Cere Unlock"}
            </button>
          </div>

          {/* Arrow */}
          <div className="hidden md:flex items-center text-slate-600 text-2xl">→</div>

          {/* Step 2 */}
          <div className={`flex-1 p-4 rounded-xl border ${canUnstake ? "border-green-700 bg-green-950/30" : "border-slate-700 bg-slate-900/30"}`}>
            <div className="flex items-center gap-2 mb-2">
              <div className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold ${canUnstake ? "bg-green-600" : "bg-slate-600"}`}>2</div>
              <span className="font-semibold text-sm">Retrage Tokens</span>
            </div>
            {unlockRequested && !canUnstake && (
              <p className="text-yellow-400 text-xs mb-2">
                ⏳ Disponibil în: {secondsToTime(unlockTimeLeft)}
              </p>
            )}
            {!unlockRequested && (
              <p className="text-slate-400 text-xs mb-3">Disponibil după 7 zile de la cerere</p>
            )}
            {canUnstake && (
              <p className="text-green-400 text-xs mb-3">✅ Gata de retras!</p>
            )}
            <button
              className="btn-primary text-sm py-2"
              onClick={unstake}
              disabled={!canUnstake || txLoading}
            >
              {txLoading ? "⏳ Procesare..." : "💰 Retrage Tokens"}
            </button>
          </div>
        </div>

        <div className="mt-4 p-3 rounded-lg bg-red-950/20 border border-red-900/30 text-xs text-red-400">
          ⚠️ <strong>Emergency Withdraw</strong>: Retrage imediat fără recompense în caz de urgență.
          <span className="ml-1 text-slate-500">(Disponibil în contract direct pe BscScan)</span>
        </div>
      </div>

      {/* ── Info ─────────────────────────────────────────────────────── */}
      <div className="card p-5 md:col-span-2">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-center text-xs">
          <div>
            <p className="text-slate-500">Contract Staking</p>
            <a href={`https://bscscan.com/address/${STAKING_ADDRESS}`} target="_blank" rel="noreferrer"
               className="text-indigo-400 hover:underline font-mono">
              {shortenAddress(STAKING_ADDRESS)}
            </a>
          </div>
          <div>
            <p className="text-slate-500">Contract Token</p>
            <a href={`https://bscscan.com/address/${TOKEN_ADDRESS}`} target="_blank" rel="noreferrer"
               className="text-indigo-400 hover:underline font-mono">
              {shortenAddress(TOKEN_ADDRESS)}
            </a>
          </div>
          <div>
            <p className="text-slate-500">APY Luna 1</p>
            <p className="text-green-400 font-bold text-sm">50%</p>
          </div>
          <div>
            <p className="text-slate-500">APY Standard</p>
            <p className="text-indigo-400 font-bold text-sm">10%</p>
          </div>
        </div>
      </div>
    </div>
  );
}
