import React from "react";
import { formatAmount } from "../utils/contracts";

export default function StatsPanel({ stats }) {
  if (!stats) return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
      {[...Array(4)].map((_, i) => (
        <div key={i} className="stat-card animate-pulse">
          <div className="h-3 rounded w-1/2 mb-3" style={{ background: "rgba(245,200,66,0.1)" }} />
          <div className="h-7 rounded w-3/4" style={{ background: "rgba(245,200,66,0.15)" }} />
        </div>
      ))}
    </div>
  );

  const { tvl, stakers, rewardsPaid, rewardPool, apy, decimals } = stats;

  const items = [
    { label: "Current APY", value: `${apy}%`, sub: apy >= 50 ? "🔥 Month 1 Bonus" : "Standard", icon: "📈" },
    { label: "Total Value Locked", value: formatAmount(tvl, decimals, 2), sub: "tokens staked", icon: "🔒" },
    { label: "Active Stakers", value: stakers.toString(), sub: "wallets", icon: "👥" },
    { label: "Reward Pool", value: formatAmount(rewardPool, decimals, 2), sub: "tokens available", icon: "💰" },
  ];

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
      {items.map((item) => (
        <div key={item.label} className="stat-card">
          <div className="flex items-center gap-2 mb-3">
            <span className="text-lg">{item.icon}</span>
            <p className="text-slate-400 text-xs font-medium uppercase tracking-wider">{item.label}</p>
          </div>
          <p className="text-2xl font-bold mb-1 gradient-text">{item.value}</p>
          <p className="text-slate-500 text-xs">{item.sub}</p>
        </div>
      ))}
    </div>
  );
}
