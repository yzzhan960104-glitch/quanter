/**
 * Vue Router 配置
 *
 * 路由规则：
 * - / → SingleBacktest（单资产回测）
 * - /portfolio → PortfolioBacktest（组合回测）
 *
 * 不引入懒加载——项目仅 2 个页面，首屏加载即可
 */
import { createRouter, createWebHistory } from 'vue-router'
import SingleBacktest from '../views/SingleBacktest.vue'
import PortfolioBacktest from '../views/PortfolioBacktest.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'single',
      component: SingleBacktest,
    },
    {
      path: '/portfolio',
      name: 'portfolio',
      component: PortfolioBacktest,
    },
  ],
})

export default router
