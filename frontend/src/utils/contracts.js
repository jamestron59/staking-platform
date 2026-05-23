import { ethers } from "ethers";

export const TOKEN_ADDRESS   = "0x6b4C5C872639DdF0e6Dfb2b7c4fe38af5b304978";
export const STAKING_ADDRESS = import.meta.env.VITE_STAKING_ADDRESS || "";
export const BSC_CHAIN_ID    = 56;
export const BSC_TESTNET_ID  = 97;

export const STAKING_ABI = [
  "function stake(uint256 amount)",
  "function requestUnlock()",
  "function unstake()",
  "function claimRewards()",
  "function emergencyWithdraw()",
  "function pendingRewards(address user) view returns (uint256)",
  "function getCurrentAPY() view returns (uint256)",
  "function getUserInfo(address user) view returns (uint256,uint256,uint256,bool,uint256)",
  "function getPlatformStats() view returns (uint256,uint256,uint256,uint256,uint256)",
  "function stakes(address) view returns (uint256,uint256,uint256,uint256)",
  "function rewardPoolBalance() view returns (uint256)",
  "function totalStaked() view returns (uint256)",
  "event Staked(address indexed user, uint256 amount, uint256 total)",
  "event Unstaked(address indexed user, uint256 amount)",
  "event RewardsClaimed(address indexed user, uint256 amount)",
];

export const TOKEN_ABI = [
  "function balanceOf(address) view returns (uint256)",
  "function decimals() view returns (uint8)",
  "function symbol() view returns (string)",
  "function approve(address spender, uint256 amount) returns (bool)",
  "function allowance(address owner, address spender) view returns (uint256)",
];

export const BSC_PARAMS = {
  chainId:          "0x38",
  chainName:        "BNB Smart Chain",
  nativeCurrency:   { name: "BNB", symbol: "BNB", decimals: 18 },
  rpcUrls:          ["https://bsc-dataseed1.binance.org/"],
  blockExplorerUrls:["https://bscscan.com/"],
};

export function getContracts(signerOrProvider) {
  return {
    staking: new ethers.Contract(STAKING_ADDRESS, STAKING_ABI, signerOrProvider),
    token:   new ethers.Contract(TOKEN_ADDRESS,   TOKEN_ABI,   signerOrProvider),
  };
}

export function formatAmount(raw, decimals = 18, precision = 4) {
  if (!raw) return "0";
  const val = Number(ethers.formatUnits(raw, decimals));
  if (val === 0) return "0";
  if (val < 0.0001) return "< 0.0001";
  return val.toLocaleString("en-US", { maximumFractionDigits: precision });
}

export function shortenAddress(addr) {
  if (!addr) return "";
  return `${addr.slice(0, 6)}...${addr.slice(-4)}`;
}

export function secondsToTime(secs) {
  if (!secs || secs <= 0) return "0s";
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (d > 0) return `${d}z ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}
