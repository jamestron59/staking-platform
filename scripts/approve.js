const { ethers } = require("ethers");
require("dotenv").config();

async function main() {
  const provider = new ethers.JsonRpcProvider("https://bsc-dataseed1.binance.org/");
  const wallet   = new ethers.Wallet(process.env.PRIVATE_KEY, provider);

  const token = new ethers.Contract(
    "0x6b4C5C872639DdF0e6Dfb2b7c4fe38af5b304978",
    ["function approve(address spender, uint256 amount) returns (bool)"],
    wallet
  );

  const amount = ethers.parseUnits("100000000", 18);
  const tx = await token.approve("0x84F9da7ad531e0D45D5C3daE3f3D5a75dDb84140", amount);
  console.log("⏳ Aprobare trimisă:", tx.hash);
  await tx.wait();
  console.log("✅ Aprobat cu succes!");
}

main().catch(console.error);