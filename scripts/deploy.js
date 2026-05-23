const hre = require("hardhat");

async function main() {
  const TOKEN_ADDRESS = "0x6b4C5C872639DdF0e6Dfb2b7c4fe38af5b304978";

  console.log("🚀 Deploy StakingPlatform pe", hre.network.name);
  console.log("🪙 Token:", TOKEN_ADDRESS);

  const [deployer] = await hre.ethers.getSigners();
  console.log("👛 Deployer:", deployer.address);

  const balance = await deployer.provider.getBalance(deployer.address);
  console.log("💰 Sold BNB:", hre.ethers.formatEther(balance), "BNB\n");

  const StakingPlatform = await hre.ethers.getContractFactory("StakingPlatform");
  const staking = await StakingPlatform.deploy(TOKEN_ADDRESS);
  await staking.waitForDeployment();

  const address = await staking.getAddress();
  console.log("✅ StakingPlatform deployed la:", address);
  console.log("🔗 BscScan:", `https://${hre.network.name === "bscMainnet" ? "" : "testnet."}bscscan.com/address/${address}`);
  console.log("\n⚠️  IMPORTANT: Adaugă această adresă în .env ca STAKING_CONTRACT_ADDRESS");
  console.log("⚠️  Nu uita să finanțezi pool-ul de recompense cu fundRewards()!");
}

main().catch((err) => { console.error(err); process.exit(1); });
