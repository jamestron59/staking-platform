import React from "react";
import { ethers } from "ethers";
import { formatAmount } from "../utils/contracts";

export default function StatsPanel({ stats }) {
  if (!stats) return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
      {[...Array(4)].map((_, i) => (
        <div key={i} className="card p-4 animate-pulse">
          <div className="h-4 bg-indigo-900 rounded w-1/2 mb-2" />
          <div className="h-6 bg-indigo-800 rounded w-3/4" />
        </div>
      ))}
    </div>
  );

  const { tvl, stakers, rewardsPaid, rewardPool, apy, decimals } = stats;

  const items = [
    {
      label: "APY Curent",
      value: `${apy}%`,
      sub:   apy >= 50 ? "🔥 Bonus luna 1" : "Standard",
      color: "text-green-400",
    },
    {
      label: "Total Staked (TVL)",
      value: formatAmount(tvl, decimals, 2),
      sub:   "tokens",
      color: "text-indigo-300",
    },
    {
      label: "Stakers",
      value: stakers.toString(),
      sub:   "utilizatori activi",
      color: "text-purple-400",
    },
    {
      label: "Pool Recompense",
      value: formatAmount(rewardPool, decimals, 2),
      sub:   "tokens disponibili",
      color: "text-yellow-400",
    },
  ];

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
      {items.map((item) => (
        <div key={item.label} className="card p-5">
          <p className="text-slate-400 text-sm mb-1">{item.label}</p>
          <p className={`text-2xl font-bold ${item.color}`}>{item.value}</p>
          <p className="text-slate-500 text-xs mt-1">{item.sub}</p>
        </div>
      ))}
    </div>
  );
}
