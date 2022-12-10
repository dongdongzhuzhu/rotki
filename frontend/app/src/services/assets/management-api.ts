import { type ActionResult } from '@rotki/common/lib/data';
import { axiosSnakeCaseTransformer } from '@/services/axios-tranformers';
import { api } from '@/services/rotkehlchen-api';
import { SupportedAssets } from '@/services/types-api';
import {
  handleResponse,
  validWithSessionAndExternalService
} from '@/services/utils';
import {
  type AssetPagination,
  type CustomAssetPagination,
  CustomAssets
} from '@/types/assets';

export const useAssetManagementApi = () => {
  async function queryAllAssets(
    pagination: Partial<AssetPagination>
  ): Promise<SupportedAssets> {
    const response = await api.instance.post<ActionResult<SupportedAssets>>(
      '/assets/all',
      axiosSnakeCaseTransformer(pagination),
      {
        validateStatus: validWithSessionAndExternalService
      }
    );

    return SupportedAssets.parse(handleResponse(response));
  }

  async function queryAllCustomAssets(
    pagination: CustomAssetPagination
  ): Promise<CustomAssets> {
    const response = await api.instance.post<ActionResult<CustomAssets>>(
      '/assets/custom',
      axiosSnakeCaseTransformer(pagination),
      {
        validateStatus: validWithSessionAndExternalService
      }
    );

    return CustomAssets.parse(handleResponse(response));
  }

  return {
    queryAllAssets,
    queryAllCustomAssets
  };
};
