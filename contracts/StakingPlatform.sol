// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/security/ReentrancyGuard.sol";
import "@openzeppelin/contracts/security/Pausable.sol";

/**
 * @title StakingPlatform
 * @notice Staking cu APY 50% prima luna, 10% dupa, unlock 7 zile
 * @dev Token: 0x6b4C5C872639DdF0e6Dfb2b7c4fe38af5b304978
 */
contract StakingPlatform is Ownable, ReentrancyGuard, Pausable {
    using SafeERC20 for IERC20;

    // ─── Constante ────────────────────────────────────────────────────────────
    IERC20  public immutable stakingToken;
    uint256 public immutable deployTime;

    uint256 public constant SECONDS_PER_YEAR  = 365 days;
    uint256 public constant HIGH_APY_DURATION = 30 days;
    uint256 public constant HIGH_APY          = 5000;  // 50% in basis points
    uint256 public constant LOW_APY           = 1000;  // 10% in basis points
    uint256 public constant APY_DENOMINATOR   = 10000;
    uint256 public constant UNLOCK_PERIOD     = 7 days;
    uint256 public constant MIN_STAKE         = 1e15;  // 0.001 token minim

    // ─── Structuri ────────────────────────────────────────────────────────────
    struct StakeInfo {
        uint256 amount;           // tokens staked
        uint256 stakedAt;         // timestamp prima staka
        uint256 lastClaimAt;      // ultima recompensa
        uint256 unlockRequestAt;  // 0 = fara cerere unlock
    }

    // ─── State ────────────────────────────────────────────────────────────────
    mapping(address => StakeInfo) public stakes;
    uint256 public totalStaked;
    uint256 public totalRewardsPaid;
    uint256 public totalStakers;

    // ─── Evenimente ──────────────────────────────────────────────────────────
    event Staked(address indexed user, uint256 amount, uint256 totalUserStake);
    event UnlockRequested(address indexed user, uint256 availableAt);
    event Unstaked(address indexed user, uint256 amount);
    event RewardsClaimed(address indexed user, uint256 amount);
    event RewardsFunded(address indexed funder, uint256 amount);
    event EmergencyWithdraw(address indexed user, uint256 amount);

    // ─── Constructor ──────────────────────────────────────────────────────────
    constructor(address _stakingToken) {
        require(_stakingToken != address(0), "Token address zero");
        stakingToken = IERC20(_stakingToken);
        deployTime   = block.timestamp;
    }

    // ══════════════════════════════════════════════════════════════════════════
    // FUNCȚII UTILIZATOR
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * @notice Stakează tokens
     * @param amount Cantitatea de tokens de stacat
     */
    function stake(uint256 amount) external nonReentrant whenNotPaused {
        require(amount >= MIN_STAKE, "Sub limita minima");

        StakeInfo storage info = stakes[msg.sender];

        // Dacă are deja tokens staked, colectăm recompensele mai întâi
        if (info.amount > 0) {
            _claimRewards(msg.sender);
            // Resetăm cererea de unlock dacă adaugă tokens
            info.unlockRequestAt = 0;
        } else {
            totalStakers++;
            info.stakedAt    = block.timestamp;
            info.lastClaimAt = block.timestamp;
        }

        stakingToken.safeTransferFrom(msg.sender, address(this), amount);

        info.amount    += amount;
        totalStaked    += amount;

        emit Staked(msg.sender, amount, info.amount);
    }

    /**
     * @notice Cere deblocare tokens (pornește countdown 7 zile)
     */
    function requestUnlock() external {
        StakeInfo storage info = stakes[msg.sender];
        require(info.amount > 0, "Nimic de desbloca");
        require(info.unlockRequestAt == 0, "Unlock deja cerut");

        info.unlockRequestAt = block.timestamp;
        emit UnlockRequested(msg.sender, block.timestamp + UNLOCK_PERIOD);
    }

    /**
     * @notice Retrage tokens după perioada de unlock
     */
    function unstake() external nonReentrant {
        StakeInfo storage info = stakes[msg.sender];
        require(info.amount > 0, "Nimic de retras");
        require(info.unlockRequestAt > 0, "Cere unlock mai intai");
        require(
            block.timestamp >= info.unlockRequestAt + UNLOCK_PERIOD,
            "Perioada de unlock nu a expirat"
        );

        // Colectăm recompensele finale
        _claimRewards(msg.sender);

        uint256 amount  = info.amount;
        totalStaked    -= amount;
        totalStakers--;
        delete stakes[msg.sender];

        stakingToken.safeTransfer(msg.sender, amount);
        emit Unstaked(msg.sender, amount);
    }

    /**
     * @notice Colectează recompensele acumulate
     */
    function claimRewards() external nonReentrant whenNotPaused {
        require(stakes[msg.sender].amount > 0, "Nimic de colectat");
        _claimRewards(msg.sender);
    }

    /**
     * @notice Retragere de urgență (fără recompense, fără unlock)
     */
    function emergencyWithdraw() external nonReentrant {
        StakeInfo storage info = stakes[msg.sender];
        require(info.amount > 0, "Nimic de retras");

        uint256 amount  = info.amount;
        totalStaked    -= amount;
        totalStakers--;
        delete stakes[msg.sender];

        stakingToken.safeTransfer(msg.sender, amount);
        emit EmergencyWithdraw(msg.sender, amount);
    }

    // ══════════════════════════════════════════════════════════════════════════
    // VIEW FUNCTIONS
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * @notice Calculează recompensele în așteptare pentru un utilizator
     */
    function pendingRewards(address user) public view returns (uint256) {
        StakeInfo memory info = stakes[user];
        if (info.amount == 0) return 0;

        uint256 fromTime         = info.lastClaimAt;
        uint256 toTime           = block.timestamp;
        uint256 highApyEnd       = deployTime + HIGH_APY_DURATION;
        uint256 rewards          = 0;

        // Perioadă APY mare (50%)
        if (fromTime < highApyEnd) {
            uint256 endHighApy    = toTime < highApyEnd ? toTime : highApyEnd;
            uint256 highApyTime   = endHighApy - fromTime;
            rewards += (info.amount * HIGH_APY * highApyTime)
                        / (APY_DENOMINATOR * SECONDS_PER_YEAR);
        }

        // Perioadă APY mic (10%)
        if (toTime > highApyEnd) {
            uint256 startLowApy   = fromTime > highApyEnd ? fromTime : highApyEnd;
            uint256 lowApyTime    = toTime - startLowApy;
            rewards += (info.amount * LOW_APY * lowApyTime)
                        / (APY_DENOMINATOR * SECONDS_PER_YEAR);
        }

        return rewards;
    }

    /**
     * @notice APY-ul curent (în basis points: 5000 = 50%)
     */
    function getCurrentAPY() public view returns (uint256) {
        return block.timestamp < deployTime + HIGH_APY_DURATION
            ? HIGH_APY
            : LOW_APY;
    }

    /**
     * @notice Soldul disponibil pentru recompense
     */
    function rewardPoolBalance() public view returns (uint256) {
        uint256 bal = stakingToken.balanceOf(address(this));
        return bal > totalStaked ? bal - totalStaked : 0;
    }

    /**
     * @notice Timpul rămas până la unlock (0 dacă nu s-a cerut sau a expirat)
     */
    function unlockTimeLeft(address user) public view returns (uint256) {
        StakeInfo memory info = stakes[user];
        if (info.unlockRequestAt == 0) return 0;
        uint256 unlockAt = info.unlockRequestAt + UNLOCK_PERIOD;
        if (block.timestamp >= unlockAt) return 0;
        return unlockAt - block.timestamp;
    }

    /**
     * @notice Returnează toate datele unui utilizator într-un singur call
     */
    function getUserInfo(address user) external view returns (
        uint256 stakedAmount,
        uint256 rewards,
        uint256 unlockLeft,
        bool    canUnstake,
        uint256 apy
    ) {
        StakeInfo memory info = stakes[user];
        stakedAmount = info.amount;
        rewards      = pendingRewards(user);
        unlockLeft   = unlockTimeLeft(user);
        canUnstake   = info.unlockRequestAt > 0
                       && block.timestamp >= info.unlockRequestAt + UNLOCK_PERIOD;
        apy          = getCurrentAPY();
    }

    /**
     * @notice Statistici globale ale platformei
     */
    function getPlatformStats() external view returns (
        uint256 tvl,
        uint256 stakers,
        uint256 rewardsPaid,
        uint256 rewardPool,
        uint256 apy
    ) {
        tvl         = totalStaked;
        stakers     = totalStakers;
        rewardsPaid = totalRewardsPaid;
        rewardPool  = rewardPoolBalance();
        apy         = getCurrentAPY();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // OWNER FUNCTIONS
    // ══════════════════════════════════════════════════════════════════════════

    /**
     * @notice Finanțează pool-ul de recompense
     */
    function fundRewards(uint256 amount) external onlyOwner {
        stakingToken.safeTransferFrom(msg.sender, address(this), amount);
        emit RewardsFunded(msg.sender, amount);
    }

    function pause()   external onlyOwner { _pause(); }
    function unpause() external onlyOwner { _unpause(); }

    // ══════════════════════════════════════════════════════════════════════════
    // INTERNAL
    // ══════════════════════════════════════════════════════════════════════════

    function _claimRewards(address user) internal {
        uint256 rewards = pendingRewards(user);
        stakes[user].lastClaimAt = block.timestamp;

        if (rewards > 0 && rewards <= rewardPoolBalance()) {
            totalRewardsPaid += rewards;
            stakingToken.safeTransfer(user, rewards);
            emit RewardsClaimed(user, rewards);
        }
    }
}
