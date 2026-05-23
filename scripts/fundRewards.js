const { ethers } = require("ethers");
require("dotenv").config();

async function main() {
  const provider = new ethers.JsonRpcProvider("https://bsc-dataseed1.binance.org/");
  const wallet   = new ethers.Wallet(process.env.PRIVATE_KEY, provider);

  const staking = new ethers.Contract(
    "0x84F9da7ad531e0D45D5C3daE3f3D5a75dDb84140",
    ["function fundRewards(uint256 amount)"],
    wallet
  );

  const amount = ethers.parseUnits("100000000", 18);
  const tx = await staking.fundRewards(amount);
  console.log("⏳ Finanțare trimisă:", tx.hash);
  await tx.wait();
  console.log("✅ Pool finanțat cu succes! 100,000,000 tokeni în pool!");
}

main().catch(console.error);