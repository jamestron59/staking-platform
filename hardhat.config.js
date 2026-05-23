require("@nomicfoundation/hardhat-toolbox");
require("dotenv").config();

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
  solidity: {
    version: "0.8.19",
    settings: {
      optimizer: { enabled: true, runs: 200 }
    }
  },
  networks: {
    bscTestnet: {
      url: "https://data-seed-prebsc-1-s1.binance.org:8545/",
      chainId: 97,
      accounts: [process.env.PRIVATE_KEY],
      gasPrice: 10_000_000_000, // 10 gwei
    },
    bscMainnet: {
      url: "https://bsc-dataseed1.binance.org/",
      chainId: 56,
      accounts: [process.env.PRIVATE_KEY],
      gasPrice: 3_000_000_000, // 3 gwei
    },
  },
  etherscan: {
  apiKey: process.env.BSCSCAN_API_KEY,
}
};
