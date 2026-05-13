const api = require('../../utils/api.js');

Page({
  data: {
    quota: { freeLeft: 1, extraUses: 0, isSubscribed: false, totalTasks: 0 },
    code: '',
    redeeming: false,
    redeemMsg: '',
    redeemSuccess: false,
    tasks: [],
  },

  onShow() {
    this.loadQuota();
    this.loadHistory();
  },

  loadQuota() {
    api.getUser().then(data => {
      this.setData({ quota: data });
    }).catch(() => {});
  },

  loadHistory() {
    api.listTasks(1).then(data => {
      this.setData({ tasks: data.tasks || [] });
    }).catch(() => {});
  },

  onCodeInput(e) {
    this.setData({ code: e.detail.value.toUpperCase(), redeemMsg: '' });
  },

  onRedeem() {
    const code = this.data.code.trim();
    if (!code) {
      this.setData({ redeemMsg: '请输入兑换码', redeemSuccess: false });
      return;
    }

    this.setData({ redeeming: true, redeemMsg: '' });

    api.redeemCode(code).then(data => {
      const msg = data.type === 'month'
        ? '月卡兑换成功！订阅有效期已延长30天 🎉'
        : '次卡兑换成功！已增加10次使用次数 🎉';
      this.setData({
        redeeming: false, code: '',
        redeemMsg: msg, redeemSuccess: true,
      });
      this.loadQuota();
    }).catch(err => {
      this.setData({
        redeeming: false,
        redeemMsg: err.message || '兑换失败',
        redeemSuccess: false,
      });
    });
  },

  onTapTask(e) {
    const taskId = e.currentTarget.dataset.id;
    wx.navigateTo({ url: '/pages/result/result?taskId=' + taskId });
  },
});
