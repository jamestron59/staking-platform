import { useState, useEffect, useCallback } from "react";
import { ethers } from "ethers";
import toast from "react-hot-toast";
import {
  getContracts, BSC_CHAIN_ID, BSC_PARAMS,
  TOKEN_ADDRESS, STAKING_ADDRESS
} from "../utils/contracts";

export function useWallet() {
  const [account,   setAccount]   = useState(null);
  const [provider,  setProvider]  = useState(null);
  const [signer,    setSigner]    = useState(null);
  const [chainId,   setChainId]   = useState(null);
  const [loading,   setLoading]   = useState(false);

  const isCorrectChain = chainId === BSC_CHAIN_ID;

  const connect = useCallback(async () => {
    if (!window.ethereum) {
      toast.error("MetaMask nu este instalat!");
      return;
    }
    setLoading(true);
    try {
      const _provider = new ethers.BrowserProvider(window.ethereum);
      await _provider.send("eth_requestAccounts", []);
      const _signer  = await _provider.getSigner();
      const _account = await _signer.getAddress();
      const network  = await _provider.getNetwork();

      setProvider(_provider);
      setSigner(_signer);
      setAccount(_account);
      setChainId(Number(network.chainId));
      toast.success("Wallet conectat!");
    } catch (err) {
      toast.error(err.message || "Conectare eșuată");
    } finally {
      setLoading(false);
    }
  }, []);

  const switchToBSC = useCallback(async () => {
    try {
      await window.ethereum.request({
        method: "wallet_switchEthereumChain",
        params: [{ chainId: "0x38" }],
      });
    } catch (err) {
      if (err.code === 4902) {
        await window.ethereum.request({
          method: "wallet_addEthereumChain",
          params: [BSC_PARAMS],
        });
      }
    }
  }, []);

  useEffect(() => {
    if (!window.ethereum) return;
    window.ethereum.on("accountsChanged", ([acc]) => {
      if (acc) setAccount(acc); else { setAccount(null); setSigner(null); }
    });
    window.ethereum.on("chainChanged", (cId) => setChainId(Number(cId)));
  }, []);

  return { account, provider, signer, chainId, isCorrectChain, loading, connect, switchToBSC };
}

export function useStaking(signer, account) {
  const [data, setData]       = useState(null);
  const [stats, setStats]     = useState(null);
  const [loading, setLoading] = useState(false);
  const [txLoading, setTxLoading] = useState(false);

  const refresh = useCallback(async () => {
    if (!signer || !account || !STAKING_ADDRESS) return;
    setLoading(true);
    try {
      const { staking, token } = getContracts(signer);
      const decimals = Number(await token.decimals());

      const [userInfo, platformStats, tokenBalance, allowance, stakeRaw] = await Promise.all([
        staking.getUserInfo(account),
        staking.getPlatformStats(),
        token.balanceOf(account),
        token.allowance(account, STAKING_ADDRESS),
        staking.stakes(account),
      ]);

      const d = BigInt(10) ** BigInt(decimals);

      setData({
        stakedAmount:    userInfo[0],
        pendingRewards:  userInfo[1],
        unlockTimeLeft:  Number(userInfo[2]),
        canUnstake:      userInfo[3],
        apy:             Number(userInfo[4]) / 100,
        tokenBalance,
        allowance,
        decimals,
        unlockRequested: Number(stakeRaw[3]) > 0,
        stakedAt:        Number(stakeRaw[1]),
      });

      setStats({
        tvl:         platformStats[0],
        stakers:     Number(platformStats[1]),
        rewardsPaid: platformStats[2],
        rewardPool:  platformStats[3],
        apy:         Number(platformStats[4]) / 100,
        decimals,
      });
    } catch (err) {
      console.error("Refresh error:", err);
    } finally {
      setLoading(false);
    }
  }, [signer, account]);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 10_000);
    return () => clearInterval(interval);
  }, [refresh]);

  // ─── Actions ───────────────────────────────────────────────────────────────
  const approve = useCallback(async (amount) => {
    setTxLoading(true);
    try {
      const { token } = getContracts(signer);
      const tx = await token.approve(STAKING_ADDRESS, amount);
      toast.loading("Aprobare în curs...", { id: "approve" });
      await tx.wait();
      toast.success("Aprobat!", { id: "approve" });
      await refresh();
    } catch (err) {
      toast.error(err.reason || err.message || "Aprobare eșuată", { id: "approve" });
    } finally {
      setTxLoading(false);
    }
  }, [signer, refresh]);

  const stake = useCallback(async (amount) => {
    setTxLoading(true);
    try {
      const { staking } = getContracts(signer);
      const tx = await staking.stake(amount);
      toast.loading("Staking în curs...", { id: "stake" });
      await tx.wait();
      toast.success("Tokens staked cu succes! 🎉", { id: "stake" });
      await refresh();
    } catch (err) {
      toast.error(err.reason || err.message || "Staking eșuat", { id: "stake" });
    } finally {
      setTxLoading(false);
    }
  }, [signer, refresh]);

  const requestUnlock = useCallback(async () => {
    setTxLoading(true);
    try {
      const { staking } = getContracts(signer);
      const tx = await staking.requestUnlock();
      toast.loading("Cerere unlock...", { id: "unlock" });
      await tx.wait();
      toast.success("Unlock cerut! 7 zile countdown pornit ⏳", { id: "unlock" });
      await refresh();
    } catch (err) {
      toast.error(err.reason || err.message || "Eroare", { id: "unlock" });
    } finally {
      setTxLoading(false);
    }
  }, [signer, refresh]);

  const unstake = useCallback(async () => {
    setTxLoading(true);
    try {
      const { staking } = getContracts(signer);
      const tx = await staking.unstake();
      toast.loading("Unstake în curs...", { id: "unstake" });
      await tx.wait();
      toast.success("Tokens retrași cu succes! 💰", { id: "unstake" });
      await refresh();
    } catch (err) {
      toast.error(err.reason || err.message || "Unstake eșuat", { id: "unstake" });
    } finally {
      setTxLoading(false);
    }
  }, [signer, refresh]);

  const claimRewards = useCallback(async () => {
    setTxLoading(true);
    try {
      const { staking } = getContracts(signer);
      const tx = await staking.claimRewards();
      toast.loading("Colectare recompense...", { id: "claim" });
      await tx.wait();
      toast.success("Recompense colectate! 🏆", { id: "claim" });
      await refresh();
    } catch (err) {
      toast.error(err.reason || err.message || "Claim eșuat", { id: "claim" });
    } finally {
      setTxLoading(false);
    }
  }, [signer, refresh]);

  return { data, stats, loading, txLoading, refresh, approve, stake, requestUnlock, unstake, claimRewards };
}
