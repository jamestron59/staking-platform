require("dotenv").config();
const express    = require("express");
const cors       = require("cors");
const helmet     = require("helmet");
const rateLimit  = require("express-rate-limit");
const { ethers } = require("ethers");

const app  = express();
const PORT = process.env.PORT || 4000;

// ─── Middleware ───────────────────────────────────────────────────────────────
app.use(helmet());
app.use(cors({ origin: process.env.FRONTEND_URL || "*" }));
app.use(express.json());
app.use(rateLimit({ windowMs: 60_000, max: 100 }));

// ─── Blockchain ───────────────────────────────────────────────────────────────
const provider = new ethers.JsonRpcProvider(
  process.env.BSC_RPC || "https://bsc-dataseed1.binance.org/"
);

const STAKING_ABI = [
  "function getPlatformStats() view returns (uint256 tvl, uint256 stakers, uint256 rewardsPaid, uint256 rewardPool, uint256 apy)",
  "function getUserInfo(address user) view returns (uint256 stakedAmount, uint256 rewards, uint256 unlockLeft, bool canUnstake, uint256 apy)",
  "function pendingRewards(address user) view returns (uint256)",
  "function getCurrentAPY() view returns (uint256)",
  "function rewardPoolBalance() view returns (uint256)",
  "function totalStaked() view returns (uint256)",
  "function totalStakers() view returns (uint256)",
  "function stakes(address) view returns (uint256 amount, uint256 stakedAt, uint256 lastClaimAt, uint256 unlockRequestAt)",
  "function unlockTimeLeft(address user) view returns (uint256)",
  "function deployTime() view returns (uint256)",
  "event Staked(address indexed user, uint256 amount, uint256 totalUserStake)",
  "event Unstaked(address indexed user, uint256 amount)",
  "event RewardsClaimed(address indexed user, uint256 amount)",
];

const TOKEN_ABI = [
  "function balanceOf(address) view returns (uint256)",
  "function decimals() view returns (uint8)",
  "function symbol() view returns (string)",
  "function name() view returns (string)",
  "function totalSupply() view returns (uint256)",
];

const stakingContract = new ethers.Contract(
  process.env.STAKING_CONTRACT_ADDRESS,
  STAKING_ABI,
  provider
);

const tokenContract = new ethers.Contract(
  "0x6b4C5C872639DdF0e6Dfb2b7c4fe38af5b304978",
  TOKEN_ABI,
  provider
);

// ─── Cache simplu (30 secunde) ────────────────────────────────────────────────
let statsCache = null;
let statsCacheTime = 0;

async function getStats() {
  const now = Date.now();
  if (statsCache && now - statsCacheTime < 30_000) return statsCache;

  const [stats, tokenSymbol, tokenName, decimals] = await Promise.all([
    stakingContract.getPlatformStats(),
    tokenContract.symbol(),
    tokenContract.name(),
    tokenContract.decimals(),
  ]);

  const divisor = BigInt(10) ** BigInt(Number(decimals));

  statsCache = {
    tvl:          Number(stats.tvl / divisor),
    tvlRaw:       stats.tvl.toString(),
    stakers:      Number(stats.stakers),
    rewardsPaid:  Number(stats.rewardsPaid / divisor),
    rewardPool:   Number(stats.rewardPool / divisor),
    apy:          Number(stats.apy) / 100,           // 5000 -> 50
    apyFormatted: `${Number(stats.apy) / 100}%`,
    token: { symbol: tokenSymbol, name: tokenName, decimals: Number(decimals) },
    contractAddress: process.env.STAKING_CONTRACT_ADDRESS,
    tokenAddress:    "0x6b4C5C872639DdF0e6Dfb2b7c4fe38af5b304978",
    updatedAt: new Date().toISOString(),
  };
  statsCacheTime = now;
  return statsCache;
}

// ══════════════════════════════════════════════════════════════════════════════
// RUTE API
// ══════════════════════════════════════════════════════════════════════════════

// GET /api/stats – statistici globale
app.get("/api/stats", async (req, res) => {
  try {
    res.json(await getStats());
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/user/:address – date utilizator
app.get("/api/user/:address", async (req, res) => {
  try {
    const { address } = req.params;
    if (!ethers.isAddress(address))
      return res.status(400).json({ error: "Adresă invalidă" });

    const decimals = Number(await tokenContract.decimals());
    const divisor  = BigInt(10) ** BigInt(decimals);

    const [info, stakeRaw] = await Promise.all([
      stakingContract.getUserInfo(address),
      stakingContract.stakes(address),
    ]);

    res.json({
      address,
      stakedAmount:   Number(info.stakedAmount / divisor),
      stakedAmountRaw: info.stakedAmount.toString(),
      pendingRewards: Number(info.rewards / divisor),
      pendingRewardsRaw: info.rewards.toString(),
      unlockTimeLeft: Number(info.unlockLeft),         // secunde
      unlockTimeLeftHours: Math.ceil(Number(info.unlockLeft) / 3600),
      canUnstake:     info.canUnstake,
      apy:            Number(info.apy) / 100,
      unlockRequested: Number(stakeRaw.unlockRequestAt) > 0,
      stakedAt:       Number(stakeRaw.stakedAt) > 0
                        ? new Date(Number(stakeRaw.stakedAt) * 1000).toISOString()
                        : null,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/history/:address – ultimele tranzacții
app.get("/api/history/:address", async (req, res) => {
  try {
    const { address } = req.params;
    if (!ethers.isAddress(address))
      return res.status(400).json({ error: "Adresă invalidă" });

    const decimals = Number(await tokenContract.decimals());
    const divisor  = BigInt(10) ** BigInt(decimals);
    const filter   = { fromBlock: -10000 };  // ultimele ~10k blocuri

    const [stakedEvts, unstakedEvts, claimedEvts] = await Promise.all([
      stakingContract.queryFilter(stakingContract.filters.Staked(address),   filter.fromBlock),
      stakingContract.queryFilter(stakingContract.filters.Unstaked(address), filter.fromBlock),
      stakingContract.queryFilter(stakingContract.filters.RewardsClaimed(address), filter.fromBlock),
    ]);

    const toEntry = (evt, type) => ({
      type,
      txHash:    evt.transactionHash,
      block:     evt.blockNumber,
      amount:    Number(evt.args.amount / divisor),
      timestamp: null, // se poate popula async cu provider.getBlock()
    });

    const history = [
      ...stakedEvts.map(e => toEntry(e, "STAKE")),
      ...unstakedEvts.map(e => toEntry(e, "UNSTAKE")),
      ...claimedEvts.map(e => toEntry(e, "CLAIM")),
    ].sort((a, b) => b.block - a.block);

    res.json({ address, history });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// GET /health
app.get("/health", (_, res) => res.json({ status: "ok", time: new Date() }));

// ─── Start ────────────────────────────────────────────────────────────────────
app.listen(PORT, () => {
  console.log(`✅ Backend pornit pe http://localhost:${PORT}`);
  console.log(`🔗 Contract: ${process.env.STAKING_CONTRACT_ADDRESS}`);
});
